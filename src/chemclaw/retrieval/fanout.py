"""Sweeping every evidence source as its own graph branch (M10, D-2026-08-10).

`gather_evidence` asks every configured retrieval source the same question and merges what comes
back. This is the map half of that, expressed as a LangGraph `Send` fan-out: one branch per source,
fanning into a single `operator.add` field.

**What this does and does not buy, because the plan claimed one thing it already had.** The plan's
case for `Send` here was latency — "sources that today serialize". They do not: `gather_evidence`
has gathered its retrievers with `asyncio.gather` since the sweep was written, and its own comment
says why ("a list comprehension made this tool cost the *sum* of their latencies when it only needs
the maximum"). So the concurrency was never the win. The win is **per-branch visibility**, and that
is not cosmetic: `D-2026-08-01-a-cap-that-starves-a-source` is a defect in which one retrieval leg
contributed *zero* surviving chunks while the sweep looked healthy in aggregate, and both competing
explanations for it were wrong. A branch that reports its own contribution makes that arithmetic
visible while it happens instead of reconstructable afterwards.

**Why a graph inside a tool, rather than the agent's own graph.** `gather_evidence` must stay one
tool with one name: `available_tool_names`, the profile narrowing, the authorization gate, the
prose contract and the safety rubric all address it by that name, and D-117 records what an omitted
name space costs. So the fan-out is a small compiled graph the tool *invokes*, not a restructuring
of the conversation graph. Measured, that still streams: a branch's `get_stream_writer()` write
surfaces in the parent agent's `astream` under the `tools:<id>` namespace, so each leg is
individually observable exactly as the plan wanted.

**It runs wherever `gather_evidence` does**, which is what keeps that one tool rather than two.
Inside a compiled graph there is always a runtime, so the branches execute identically under a CLI
sweep or a Temporal activity; only the *visibility* differs, because nothing is consuming a custom
stream there. A tool whose results depended on who invoked it would be the worse trade.

**Fan-in order is not the order the branches finish, and that is load-bearing.** `operator.add`
appends whichever branch completes first, while both merge modes downstream depend on stable input
order: `reciprocal_rank_fusion` takes a note's representative chunk from "the first one encountered
across the lists (stable input order)", and `_interleave_dedup` round-robins in list order, so the
sequence decides which duplicate survives. Nondeterministic order would make one sweep's evidence
differ from the next for no reason a chemist could see — a reproducibility problem,
which is the same argument `connectors/registry._bundle_dirs` sorts for. So every branch carries
the index of the source it ran, and the fan-in restores that order before returning.
"""

import logging
import operator
import time
from collections.abc import Iterable
from typing import Annotated, Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from typing_extensions import TypedDict

from chemclaw.core.metrics_bridge import degraded, record_metric
from chemclaw.core.turn_signals import stream_writer_or_none
from chemclaw.retrieval.evidence import EvidenceChunk, RetrieverSkip, SourceRetriever

logger = logging.getLogger(__name__)

# What a branch is handed and what it reads its retriever from. The retrievers travel in the
# invocation config rather than in state deliberately: graph state is msgpack-encoded through a
# type allowlist when it is checkpointed, and a `SourceRetriever` is a live object holding a
# database handle — something that must never end up in a checkpoint even though this particular
# graph has none.
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
    # Sources that *declined* (`RetrieverSkip`), as `(name, reason)`. A third channel because it
    # is a third fact: "asked, found nothing" (an empty ranked list), "could not ask" (`failed`),
    # and "would not ask, and said why" are three answers with three different fixes.
    skipped: Annotated[list[tuple[str, str]], operator.add]


async def _sweep(state: BranchState, config: RunnableConfig) -> dict[str, Any]:
    """Run one source and report what it contributed, on its own branch.

    The report is the point of the branch existing. A source that returns nothing is
    indistinguishable from a source nobody asked in an aggregate hit-list, and telling those two
    apart is what `D-2026-08-01-a-cap-that-starves-a-source` needed and did not have.

    A branch that *raises* costs its own source and not the sweep: an unreachable index or a
    retriever whose backing store is down should degrade the evidence, exactly as an unreachable
    connector costs its tools and not the turn. The failure is logged and counted, never swallowed
    silently.

    **And it is reported as a failure rather than as a zero.** Both paths end with an empty list, so
    for as long as the report carried only a count the two converged on one indistinguishable
    `0` — the exact collapse this fan-out exists to undo one level up, reintroduced one level down.
    "This source had nothing to say" is a fact about the corpus and "this source is broken" is a
    fact about the deployment; they have different fixes and different urgencies, and a surface
    that renders them identically sends a chemist looking for the wrong one.
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
        # A decline, not an outage: no failure counter, no degradation record — but never a bare
        # zero either. The reason travels to the caller so the model can render "the share leg
        # requires an entitled actor" instead of "nothing on file".
        record_metric(
            lambda m: m.increment("chemclaw_evidence_source_skips_total", 1, {"source": name})
        )
        _report(name, 0, failed=False, skipped=skip.reason)
        return {"ranked": [(index, [])], "failed": [], "skipped": [(name, skip.reason)]}
    except Exception as exc:
        _record_seconds(name, time.perf_counter() - started)
        # Through `degraded()` rather than a bare `logger.exception` plus a private counter: this is
        # the repository's chokepoint for "we continued with less", and a swallow that does not go
        # through it is invisible to `chemclaw_degraded_total` and to `tests/test_degraded.py`,
        # which reads the subsystem names out of the source and pins the set.
        #
        # **The exception's type is in the line**, because it is what the retrievers used to log
        # before they were made to raise. Their argument for catching was that a retriever knows
        # the difference between a transient outage and a missing driver — which is true, and was
        # a property of the *log* rather than of the answer they returned. Naming the type here
        # keeps that classification while the fact of the failure travels on to the caller in
        # `failed`, which is the channel the retrievers' own handlers used to empty.
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
    return {"ranked": [(index, list(chunks))], "failed": [], "skipped": []}


def _record_seconds(name: str, seconds: float) -> None:
    """Record how long one source took to answer — on every path, including the ones that failed.

    **There was no retrieval latency anywhere in this system**, which leaves the two states a
    chemist most needs told apart looking identical from outside: a vector store that is timing out
    and a vector store that is empty both return `[]`, and both booked an honest `chunks=0`. The
    duration is the field that separates them, and it is only informative if the failing path
    records it too — a leg that raises after twenty seconds and one that raises immediately are a
    different fault with a different fix.
    """
    record_metric(
        lambda m: m.observe("chemclaw_evidence_source_seconds", seconds, {"source": name})
    )


def record_kept_chunks(
    kept: Iterable[EvidenceChunk],
    handed: Iterable[tuple[str, list[EvidenceChunk]]],
) -> None:
    """Count, per source, how much of what it **handed over** reached the answer.

    `chemclaw_evidence_source_chunks_total` counts what a retriever handed over — `EvidenceSweep`
    says so in as many words ("Counts are pre-merge") — which is measured before RRF or the
    round-robin interleave and before the budget cap. So it does not cover the defect its own ADR
    names: `D-2026-08-01-a-cap-that-starves-a-source` measured *surviving* chunks (graph 38,
    lexical 0, vector 2), and under a pre-merge counter a leg that contributes thirty chunks and
    survives none reads as perfectly healthy. The alert is the ratio `kept / chunks` going to zero
    for one source, which is that ADR's table expressed as a number a dashboard can hold.

    **Counted from the source's own hit-list, not from the surviving chunk's `retriever` label**,
    and that is the whole of the correction. Both merge modes collapse a note found twice —
    `_interleave_dedup` keeps the first `(note, content)` it meets, RRF keeps "the first one
    encountered across the lists (stable input order)" — so the surviving chunk carries the
    *earlier* source's name and a label-counted numerator credits list order rather than
    contribution. Measured over the shipped 38-note corpus with `graph,vector,lexical` enabled
    against a real note index, ten research queries, **nothing truncated on any of them**: every
    hit every leg handed over reached the answer, and the label-counted ratio still read
    graph 38/38, vector 42/73 and **lexical 8/73** in `hybrid` mode — 0.00 on four queries out of
    ten — with graph 25/38, vector 38/73, lexical 25/73 in `graph` mode. A leg that agrees with an
    earlier one was indistinguishable from a leg the cap had emptied, which is the one distinction
    this counter exists to make.

    So a source is credited for each of its own hits whose note is in the answer, however that
    note got there. The ratio then stays in `[0, 1]` per source, and the sum across sources may
    exceed the number of chunks returned — deliberately, because corroboration is not waste.

    **Every asked source is booked**, including at zero, and that is a deliberate exception to this
    registry's rule against invented zero series. It is not invented: a source that was asked and
    kept nothing is an *observation*, and without the seeded series the ratio has no denominator at
    exactly the moment it matters — a starved leg would be absent from the metric rather than
    reading zero.

    Args:
        kept: The chunks that reached the caller, after merging and both budget caps, **before
            framing** — `gather_evidence` rewrites `source_note_id` on its way out, and an id
            compared across that rewrite is the silent-zero this function is fixing.
        handed: `(name, hits)` per source, exactly as the sweep returned them.
    """
    surviving = {chunk.source_note_id for chunk in kept}
    for name, hits in handed:
        _record_kept(name, sum(1 for hit in hits if hit.source_note_id in surviving))


def _record_kept(name: str, count: int) -> None:
    """Add one source's surviving-hit count — named, so the loop above cannot capture late."""
    record_metric(
        lambda m: m.increment("chemclaw_evidence_source_kept_total", count, {"source": name})
    )


def _report(name: str, found: int, *, failed: bool, skipped: str | None = None) -> None:
    """Publish one branch's contribution, to whoever is watching this turn.

    Two audiences, one fact. `get_stream_writer` reaches a surface watching the turn live, which is
    what makes a starved leg visible *while* the sweep runs; the counter is what makes it alertable
    across turns, because a leg that returns nothing on one query is normal and a leg that returns
    nothing on every query is a broken deployment. The writer call is guarded because this graph is
    also invoked outside any streaming consumer (the CLI, a Temporal activity), and
    a sweep must not fail for want of an audience.

    `failed` is what tells a dark leg from a broken one, on both audiences at once. On the stream it
    rides beside the count, because a consumer reading `chunks == 0` cannot recover the difference
    afterwards. For the counters the same distinction is a *label*: the failure counter is labelled
    with the source exactly as the chunk counter already is, so the two series can be joined —
    unlabelled, "which source is dark" and "which source is raising" were two numbers that could
    only be correlated by guessing, which is the shape of blindness
    `D-2026-08-01-a-cap-that-starves-a-source` was found in.
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
        # `failed` is on the event because without it this branch published
        # `{"evidence_source": "graph", "chunks": 0}` for a source that ran fine and matched
        # nothing *and* for a source whose database was unreachable — byte-identical.
        event: dict[str, Any] = {"evidence_source": name, "chunks": found, "failed": failed}
        if skipped is not None:
            event["skipped"] = skipped
        writer(event)


def _fan(state: FanState, config: RunnableConfig) -> list[Send]:
    """One `Send` per configured source — the map step.

    Reads the source list from the config rather than from state for the reason `_SOURCES` gives:
    the retrievers are live objects. An empty source list fans out to nothing and the sweep returns
    empty, which is a real deployment (every source disabled) rather than an error.
    """
    sources = dict(config.get("configurable") or {}).get(_SOURCES, [])
    return [Send("sweep", BranchState(index=index)) for index in range(len(sources))]


def _build() -> Any:
    """Compile the fan-out once per process — it is a constant, and compiling is not free."""
    graph = StateGraph(FanState)
    graph.add_node("sweep", _sweep)
    graph.add_conditional_edges(START, _fan, ["sweep"])
    graph.add_edge("sweep", END)
    return graph.compile()


_FANOUT = _build()


async def sweep_sources(
    sources: list[tuple[str, SourceRetriever]],
    query: str,
    filters: dict[str, Any],
) -> tuple[list[list[EvidenceChunk]], list[str], dict[str, str]]:
    """Ask every source the same question at once; return their hit-lists **and what failed**.

    Args:
        sources: `(name, retriever)` per source, in the order the merge downstream expects. The
            name is what a branch reports itself as and what the per-source counter is labelled
            with, so it must be the retriever's own name — a label invented here would not match
            the `retriever` field on the chunks it returns.
        query: What to ask each source.
        filters: The graph filters (type/tag/date window), applied by the sources that honour them.

    Returns:
        `(ranked_lists, failed_names, skipped)`. One ranked list per source, **in the order
        `sources` was given** — never in completion order; both merge modes downstream depend on
        that (see the module docstring). `failed_names` holds the sources that raised; `skipped`
        maps each source that declined (`RetrieverSkip`) to its stated reason.

    **Returning the failures is the whole reason this signature changed.** It used to return the
    hit-lists alone, so a source whose database was unreachable was indistinguishable from a source
    that ran and matched nothing: both contributed `[]`. `gather_evidence` then handed the model an
    empty list under a docstring that tells it, in as many words, that empty means *nothing on
    file, never invented* — so a chemist asking "have we run this nitration before?" during a
    Postgres blip was told the company has no prior art, confidently, with nothing anywhere saying
    a source was down. A caller cannot make that distinction from a value that does not carry it.
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
