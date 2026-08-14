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
from typing import Annotated, Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from typing_extensions import TypedDict

from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.turn_signals import stream_writer_or_none
from chemclaw.retrieval.evidence import EvidenceChunk, SourceRetriever

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
    """

    ranked: Annotated[list[tuple[int, list[EvidenceChunk]]], operator.add]


async def _sweep(state: BranchState, config: RunnableConfig) -> dict[str, Any]:
    """Run one source and report what it contributed, on its own branch.

    The report is the point of the branch existing. A source that returns nothing is
    indistinguishable from a source nobody asked in an aggregate hit-list, and telling those two
    apart is what `D-2026-08-01-a-cap-that-starves-a-source` needed and did not have.

    A branch that *raises* costs its own source and not the sweep: an unreachable index or a
    retriever whose backing store is down should degrade the evidence, exactly as an unreachable
    connector costs its tools and not the turn. The failure is logged and counted, never swallowed
    silently.
    """
    configurable: dict[str, Any] = dict(config.get("configurable") or {})
    sources: list[tuple[str, SourceRetriever]] = configurable[_SOURCES]
    index = state["index"]
    name, retriever = sources[index]
    try:
        chunks = await retriever.retrieve(configurable[_QUERY], configurable[_FILTERS])
    except Exception:
        logger.exception("evidence source %r failed; the sweep continues without it", name)
        record_metric(lambda m: m.increment("chemclaw_evidence_source_failures_total", 1))
        chunks = []
    _report(name, len(chunks))
    return {"ranked": [(index, list(chunks))]}


def _report(name: str, found: int) -> None:
    """Publish one branch's contribution, to whoever is watching this turn.

    Two audiences, one fact. `get_stream_writer` reaches a surface watching the turn live, which is
    what makes a starved leg visible *while* the sweep runs; the counter is what makes it alertable
    across turns, because a leg that returns nothing on one query is normal and a leg that returns
    nothing on every query is a broken deployment. The writer call is guarded because this graph is
    also invoked outside any streaming consumer (the CLI, a Temporal activity), and
    a sweep must not fail for want of an audience.
    """
    record_metric(
        lambda m: m.increment("chemclaw_evidence_source_chunks_total", found, {"source": name})
    )
    # Through the shared guard, not a second `except` list: two sites catching different sets for
    # one upstream call is how a change breaks one of them silently (`turn_signals` says which).
    writer = stream_writer_or_none()
    if writer is None:  # no graph runtime, or nothing consuming a custom stream
        logger.debug("evidence source %r contributed %d chunk(s)", name, found)
    else:
        writer({"evidence_source": name, "chunks": found})


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
) -> list[list[EvidenceChunk]]:
    """Ask every source the same question at once; return their ranked hit-lists, in source order.

    Args:
        sources: `(name, retriever)` per source, in the order the merge downstream expects. The
            name is what a branch reports itself as and what the per-source counter is labelled
            with, so it must be the retriever's own name — a label invented here would not match
            the `retriever` field on the chunks it returns.
        query: What to ask each source.
        filters: The graph filters (type/tag/date window), applied by the sources that honour them.

    Returns:
        One ranked list per source, **in the order `sources` was given** — never in completion
        order. Both merge modes downstream depend on that (see the module docstring).
    """
    if not sources:
        return []
    state: FanState = await _FANOUT.ainvoke(
        {"ranked": []},
        {
            "configurable": {
                _SOURCES: sources,
                _QUERY: query,
                _FILTERS: filters,
            }
        },
    )
    by_index = dict(state["ranked"])
    return [by_index.get(index, []) for index in range(len(sources))]
