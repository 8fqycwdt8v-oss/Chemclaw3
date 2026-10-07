"""Sweeping every evidence source as its own graph branch (D-2026-08-10).

`gather_evidence` asks every configured source the same question; this is the map half, as a
LangGraph `Send` fan-out into one `operator.add` field. The gain is per-branch visibility (each
leg reports its own contribution, failure or decline, and streams under the parent's
`tools:<id>` namespace), not concurrency. It is a small graph the tool invokes, so
`gather_evidence` stays one tool with one name, and it runs identically under a CLI or a Temporal
activity.

Fan-in order is restored to source order, not completion order: both merge modes downstream take
the first occurrence of a note, so a nondeterministic order would change which duplicate survives.
"""

import logging
import operator
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from typing_extensions import TypedDict

from chemclaw.core.metrics_bridge import degraded, record_metric
from chemclaw.core.turn_signals import stream_writer_or_none
from chemclaw.retrieval.evidence import EvidenceChunk, RetrieverSkip, SourceRetriever

logger = logging.getLogger(__name__)

# Retrievers travel in the invocation config, not state: they hold live database handles and must
# never be checkpointed.
_SOURCES = "chemclaw_evidence_sources"
_QUERY = "chemclaw_evidence_query"
_FILTERS = "chemclaw_evidence_filters"


class BranchState(TypedDict):
    """One branch's input: which source to run, by index into the invocation's source list."""

    index: int


class FanState(TypedDict):
    """The sweep's state — every branch's ranked hits, tagged with the source that produced them.

    `ranked` is `operator.add` over `(index, chunks)` pairs rather than over the chunk lists
    themselves, because the fan-in has to be re-ordered afterwards and a bare concatenation loses
    the only thing that could re-order it (see the module docstring).

    `failed` carries the names of the sources that *raised*. It is a separate channel rather than a
    sentinel inside `ranked` because the two are different facts and the caller acts on them
    differently: an empty hit-list means "asked, found nothing", and a name here means "could not
    ask". Collapsing them is the defect this channel exists to end — see `sweep_sources`.
    """

    ranked: Annotated[list[tuple[int, list[EvidenceChunk]]], operator.add]
    failed: Annotated[list[str], operator.add]
    # Sources that declined (`RetrieverSkip`), as `(name, reason)`: "found nothing", "could not ask"
    # and "would not ask" are three answers with three fixes.
    skipped: Annotated[list[tuple[str, str]], operator.add]


async def _sweep(state: BranchState, config: RunnableConfig) -> dict[str, Any]:
    """Run one source and report what it contributed, on its own branch.

    A branch that raises costs only its own source: the failure is logged, counted and reported as
    a failure, never as a zero, because "nothing on file" and "this source is broken" need different
    fixes.
    """
    configurable: dict[str, Any] = dict(config.get("configurable") or {})
    sources: list[tuple[str, SourceRetriever]] = configurable[_SOURCES]
    index = state["index"]
    name, retriever = sources[index]
    started = time.perf_counter()
    try:
        chunks = await retriever.retrieve(configurable[_QUERY], configurable[_FILTERS])
    except RetrieverSkip as skip:
        _record_seconds(name, time.perf_counter() - started)
        # A decline, not an outage: no failure counter, but the reason travels so the model can say
        # why.
        record_metric(
            lambda m: m.increment("chemclaw_evidence_source_skips_total", 1, {"source": name})
        )
        _report(name, 0, failed=False, skipped=skip.reason)
        return {"ranked": [(index, [])], "failed": [], "skipped": [(name, skip.reason)]}
    except Exception as exc:
        _record_seconds(name, time.perf_counter() - started)
        # Through `degraded()`, the chokepoint `chemclaw_degraded_total` and
        # `tests/test_degraded.py` read. The exception type is in the line (it distinguishes an
        # outage from a missing driver), while the failure itself travels to the caller in `failed`.
        degraded(
            logger,
            "evidence_source",
            "evidence source %r failed with %s; the sweep continues",
            name,
            type(exc).__name__,
        )
        logger.debug("evidence source %r failure detail", name, exc_info=True)
        record_metric(
            lambda m: m.increment("chemclaw_evidence_source_failures_total", 1, {"source": name})
        )
        _report(name, 0, failed=True)
        return {"ranked": [(index, [])], "failed": [name], "skipped": []}
    _record_seconds(name, time.perf_counter() - started)
    _report(name, len(chunks), failed=False)
    # Not `list(chunks)`: a `Hits` carries its pre-cut count, which a copy would discard.
    return {"ranked": [(index, chunks)], "failed": [], "skipped": []}


def _record_seconds(name: str, seconds: float) -> None:
    """Record how long one source took to answer, on every path, including the ones that failed.

    Duration separates a timing-out store from an empty one (both return `[]`), and a slow failure
    from a fast one.
    """
    record_metric(
        lambda m: m.observe("chemclaw_evidence_source_seconds", seconds, {"source": name})
    )


def record_kept_chunks(
    kept: Iterable[EvidenceChunk], contributions: Mapping[str, Sequence[EvidenceChunk]]
) -> None:
    """Count the chunks that **survived** the merge and both caps, per source that surfaced them.

    `chemclaw_evidence_source_chunks_total` counts what a leg handed over (pre-merge); this is the
    survivor half, so `kept / chunks` going to zero for one source is the starvation alert. A note
    is credited to every source that surfaced it, not just its first finder (`chunk.retriever`),
    since legs agreeing is the healthy case. Every asked source is seeded at zero: a source that was
    asked and kept nothing is an observation, and the ratio needs it.

    Args:
        kept: The chunks that reached the caller, after merging and both budget caps.
        contributions: What each asked source handed over, by name. Its keys are the asked set, so
            a source that returned nothing is still present as a zero.
    """
    survivors = {(chunk.source_note_id, chunk.content) for chunk in kept}
    for name, offered in contributions.items():
        _record_kept(
            name,
            sum(1 for chunk in offered if (chunk.source_note_id, chunk.content) in survivors),
        )
    # A chunk whose `retriever` is not among the asked names would otherwise be dropped silently;
    # counting it keeps the two series comparable rather than quietly under-reporting the numerator.
    unattributed: Counter[str] = Counter(
        chunk.retriever for chunk in kept if chunk.retriever not in contributions
    )
    for name, count in unattributed.items():
        _record_kept(name, count)


def _record_kept(name: str, count: int) -> None:
    """Add one source's surviving-chunk count — named, so the loop above cannot capture late."""
    record_metric(
        lambda m: m.increment("chemclaw_evidence_source_kept_total", count, {"source": name})
    )


def _report(name: str, found: int, *, failed: bool, skipped: str | None = None) -> None:
    """Publish one branch's contribution, to whoever is watching this turn.

    The stream writer makes a starved leg visible live; the counter makes it alertable across turns.
    The writer is guarded because the graph also runs with no streaming consumer. `failed` rides on
    the event and labels the failure counter by source, so "dark" and "raising" are distinguishable
    on both audiences.
    """
    record_metric(
        lambda m: m.increment("chemclaw_evidence_source_chunks_total", found, {"source": name})
    )
    # Through the shared guard, not a second `except` list: two sites catching different sets for
    # one upstream call is how a change breaks one of them silently (`turn_signals` says which).
    writer = stream_writer_or_none()
    if writer is None:  # no graph runtime, or nothing consuming a custom stream
        logger.debug(
            "evidence source %r contributed %d chunk(s)%s",
            name,
            found,
            " after failing" if failed else "",
        )
    else:
        # `failed` distinguishes a source that matched nothing from one that could not be reached.
        event: dict[str, Any] = {"evidence_source": name, "chunks": found, "failed": failed}
        if skipped is not None:
            event["skipped"] = skipped
        writer(event)


def _fan(state: FanState, config: RunnableConfig) -> list[Send]:
    """One `Send` per configured source: the map step.

    Reads sources from the config (see `_SOURCES`). No sources fans out to nothing, which is a real
    deployment, not an error.
    """
    sources = dict(config.get("configurable") or {}).get(_SOURCES, [])
    return [Send("sweep", BranchState(index=index)) for index in range(len(sources))]


def _build() -> Any:
    """Compile the fan-out once per process.

    `checkpointer=False`, because `None` means "inherit": invoked inside a turn, it would checkpoint
    retrieved corpus onto the chemist's thread under no cap, and nothing ever resumes a fan-out.
    """
    graph = StateGraph(FanState)
    graph.add_node("sweep", _sweep)
    graph.add_conditional_edges(START, _fan, ["sweep"])
    graph.add_edge("sweep", END)
    return graph.compile(checkpointer=False)


_FANOUT = _build()


async def sweep_sources(
    sources: list[tuple[str, SourceRetriever]],
    query: str,
    filters: dict[str, Any],
) -> tuple[list[list[EvidenceChunk]], list[str], dict[str, str]]:
    """Ask every source the same question at once; return their hit-lists **and what failed**.

    Args:
        sources: `(name, retriever)` per source, in the order the merge downstream expects. The
            name labels the branch and its counter, so it must be the retriever's own name.
        query: What to ask each source.
        filters: The graph filters (type/tag/date window), applied by the sources that honour them.

    Returns:
        `(ranked_lists, failed_names, skipped)`. One ranked list per source, **in the order
        `sources` was given**, never completion order. `failed_names` holds the sources that
        raised (so an outage is never presented as "nothing on file"); `skipped` maps each source
        that declined to its stated reason.
    """
    if not sources:
        return [], [], {}
    state: FanState = await _FANOUT.ainvoke(
        {"ranked": [], "failed": [], "skipped": []},
        {
            "configurable": {
                _SOURCES: sources,
                _QUERY: query,
                _FILTERS: filters,
            }
        },
    )
    by_index = dict(state["ranked"])
    return (
        [by_index.get(index, []) for index in range(len(sources))],
        list(state["failed"]),
        dict(state["skipped"]),
    )
