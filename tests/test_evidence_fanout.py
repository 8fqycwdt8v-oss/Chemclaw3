"""The evidence sweep as a `Send` fan-out, and the source balance it exists for.

The fan-out's own properties (order, degradation, per-branch reporting) are asserted directly.
`test_the_starved_source_measurement_rerun` rebuilds a mixed sweep against the chunk cap and
checks that every source with hits survives it, printing the per-source split.
"""

import asyncio
from typing import Any

import pytest

from chemclaw.retrieval.evidence import EvidenceChunk
from chemclaw.retrieval.fanout import sweep_sources


class _Retriever:
    """A source that returns a fixed hit-list, optionally after a delay or by raising."""

    def __init__(
        self,
        name: str,
        count: int,
        *,
        delay: float = 0.0,
        score: float = 0.5,
        fails: bool = False,
    ) -> None:
        """Build a source that behaves the one way this test needs it to."""
        self.name = name
        self._count = count
        self._delay = delay
        self._score = score
        self._fails = fails

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return this source's hits, best first, after however long it takes."""
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fails:
            raise RuntimeError(f"{self.name} is unreachable")
        return [
            EvidenceChunk(
                content=f"{self.name} hit {index}",
                source_note_id=f"{self.name}-{index}",
                retriever=self.name,
                score=self._score,
            )
            for index in range(self._count)
        ]


def _swept(sources: list[_Retriever]) -> list[list[EvidenceChunk]]:
    """Run one sweep and return its per-source ranked lists.

    Drops the failed-source names from `sweep_sources`' return; that channel has its own tests.
    """
    lists, _failed, _skipped = asyncio.run(sweep_sources([(s.name, s) for s in sources], "q", {}))
    return lists


def test_the_fan_in_is_in_source_order_not_completion_order() -> None:
    """The fan-in is in source order, not completion order.

    The first source is made slowest. Both merge modes read the lists positionally, so order
    depending on which database answered first would return different evidence for one question
    across runs.
    """
    slow = _Retriever("graph", 1, delay=0.05)
    fast = _Retriever("lexical", 1)
    lists = _swept([slow, fast])
    assert [chunks[0].retriever for chunks in lists] == ["graph", "lexical"]


def test_every_source_gets_its_own_branch_and_they_run_together() -> None:
    """The map step really fans out: three sources, three branches, all in flight at once.

    Asserted as observed overlap rather than as a wall-clock threshold, so it measures concurrency
    instead of measuring how loaded the machine is.
    """
    running = 0
    peak = 0

    class _Occupancy(_Retriever):
        """Records how many peers were in flight alongside it."""

        async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            try:
                return await super().retrieve(query, filters)
            finally:
                running -= 1

    _swept([_Occupancy(name, 1, delay=0.05) for name in ("a", "b", "c")])
    assert peak == 3, f"branches did not overlap; peak occupancy was {peak}"


def test_a_source_that_fails_costs_its_own_leg_and_not_the_sweep() -> None:
    """A source that fails costs its own leg and not the sweep.

    Its empty list stays in its own position, so later sources keep their places and the merge is
    unaffected. The failure itself is reported on a separate channel
    (`test_a_failed_leg_and_an_empty_leg_report_differently`).
    """
    lists = _swept(
        [_Retriever("graph", 2), _Retriever("lexical", 1, fails=True), _Retriever("dense", 2)]
    )
    assert [len(chunks) for chunks in lists] == [2, 0, 2]
    assert [chunk.retriever for chunk in lists[2]] == ["dense", "dense"]


def _reports(sources: list[_Retriever]) -> list[dict[str, Any]]:
    """Every custom-stream payload one sweep publishes, in arrival order."""
    from chemclaw.retrieval.fanout import _FANOUT, _FILTERS, _QUERY, _SOURCES

    async def _stream() -> list[dict[str, Any]]:
        return [
            payload
            async for payload in _FANOUT.astream(
                {"ranked": []},
                {
                    "configurable": {
                        _SOURCES: [(s.name, s) for s in sources],
                        _QUERY: "q",
                        _FILTERS: {},
                    }
                },
                stream_mode="custom",
            )
        ]

    return asyncio.run(_stream())


def test_a_failed_leg_and_an_empty_leg_report_differently() -> None:
    """A failed leg and an empty leg report differently on the turn's event stream.

    "The corpus has nothing on this" is an answer; "the index is down" is an outage. Asserted as a
    difference between two legs in one sweep, since the defect would be the two payloads being
    equal.
    """
    sources = [_Retriever("graph", 2), _Retriever("lexical", 0), _Retriever("dense", 1, fails=True)]
    by_source = {payload["evidence_source"]: payload for payload in _reports(sources)}

    quiet, broken = by_source["lexical"], by_source["dense"]
    assert quiet["chunks"] == broken["chunks"] == 0, "the precondition: both contributed nothing"
    assert quiet != broken, "a failed leg is still indistinguishable from an empty one"
    assert quiet["failed"] is False
    assert broken["failed"] is True
    assert by_source["graph"] == {"evidence_source": "graph", "chunks": 2, "failed": False}


def test_the_failure_counter_names_the_source_that_failed() -> None:
    """The failure counter names the source that failed.

    Labelled `{source}` like the chunk counter, so the two series can be joined across turns. The
    source names are unique to this test because the registry is process-global and other tests
    sweep failing sources named `graph`; shared names would make the absence half order-dependent.
    """
    from chemclaw.core.metrics import METRICS

    healthy, broken = "fanout-labelled-healthy", "fanout-labelled-broken"
    _swept([_Retriever(healthy, 1), _Retriever(broken, 1, fails=True)])
    rendered = METRICS.render()

    assert f'chemclaw_evidence_source_failures_total{{source="{broken}"}}' in rendered
    assert f'chemclaw_evidence_source_failures_total{{source="{healthy}"}}' not in rendered


def test_no_sources_is_an_empty_sweep_and_not_an_error() -> None:
    """Every source disabled is a real deployment, not a misconfiguration to raise on."""
    assert _swept([]) == []


def test_each_branch_reports_what_it_contributed() -> None:
    """Each branch reports what it contributed, while the sweep runs.

    A source returning nothing and one nobody asked look the same in an aggregate list; the branch
    reports zero. Read off the graph's custom stream, as a surface receives it in a real turn.
    """
    sources = [_Retriever("graph", 3), _Retriever("lexical", 0), _Retriever("dense", 2)]
    reported = {item["evidence_source"]: item["chunks"] for item in _reports(sources)}
    assert reported == {"graph": 3, "lexical": 0, "dense": 2}


def test_the_starved_source_measurement_rerun(capsys: pytest.CaptureFixture[str]) -> None:
    """Every source that had hits survives the chunk cap.

    A sweep of 45 graph hits at 0.8 confidence, 8 lexical at low `ts_rank` and 7 dense at mid
    cosine, against a 40-chunk cap. Asserted as the property rather than exact counts, which would
    freeze the round-robin's arithmetic; the defect is a zero. The split is printed for the record.
    """
    from chemclaw.agent.research_tools import _interleave_dedup
    from chemclaw.core.config import settings

    lists = _swept(
        [
            _Retriever("graph", 45, score=0.8),
            _Retriever("lexical", 8, score=0.05),
            _Retriever("dense", 7, score=0.75),
        ]
    )
    assert [len(chunks) for chunks in lists] == [45, 8, 7], "the sweep itself lost hits"

    cap = settings.gather_evidence_max_chunks
    kept = _interleave_dedup(lists)[:cap]
    split = {
        name: sum(1 for chunk in kept if chunk.retriever == name)
        for name in ("graph", "lexical", "dense")
    }

    with capsys.disabled():
        print(
            f"\nstarved-source re-measurement (cap={cap}): "
            f"{split['graph']} graph / {split['lexical']} lexical / {split['dense']} dense "
            f"— ADR recorded 38/0/2 (flat union) and 40/0/0 (no score sort)"
        )

    assert split["lexical"] > 0, "the lexical leg is starved again"
    assert split["dense"] > 0, "the dense leg is starved again"
    assert sum(split.values()) == min(cap, 60)


def test_a_branch_report_reaches_the_turn_event_stream() -> None:
    """A branch report reaches the turn event stream, end to end.

    The branch runs inside a tool, inside a `Send` branch of the agent's model→tools edge, and the
    report must cross both boundaries; asserting `_custom_event` alone would skip that. A raising
    third source checks that `failed` crosses them too.
    """
    from langchain_core.tools import StructuredTool

    from chemclaw.agent.audit import NullAuditSink
    from chemclaw.agent.langgraph_agent import build_langgraph_agent
    from chemclaw.api.events import EvidenceSourceEvent
    from chemclaw.api.graph_stream import graph_events
    from chemclaw.api.runner_trace import ToolCallTrace
    from tests.fakes_langgraph import ScriptedChatModel

    async def sweep(query: str) -> str:
        """Stand in for `gather_evidence`: the same fan-out, sources that need no database."""
        legs = (
            _Retriever("graph", 4),
            _Retriever("lexical", 0),
            _Retriever("dense", 3, fails=True),
        )
        lists, _failed, _skipped = await sweep_sources([(s.name, s) for s in legs], query, {})
        return f"{sum(len(chunks) for chunks in lists)} chunks"

    class _Usage:
        def add(self, _usage: Any) -> None:
            """The ledger's shape; this test does not assert on tokens."""

    async def _turn() -> list[Any]:
        graph = build_langgraph_agent(
            ScriptedChatModel([{"name": "sweep", "args": {"query": "q"}}, "done"]),
            audit_sink=NullAuditSink(),
            connectors=[
                StructuredTool.from_function(
                    coroutine=sweep, name="sweep", description="sweep the sources"
                )
            ],
        )
        return [
            event
            async for event in graph_events(
                graph,
                "what do we know?",
                config={"configurable": {"thread_id": "t-fanout"}},
                trace=ToolCallTrace(),
                on_signal=lambda _s: None,
                usage=_Usage(),
            )
        ]

    reports = [e for e in asyncio.run(_turn()) if isinstance(e, EvidenceSourceEvent)]
    assert {r.source: (r.chunks, r.failed) for r in reports} == {
        "graph": (4, False),
        "lexical": (0, False),
        "dense": (0, True),
    }


def test_a_failed_source_is_named_on_the_channel_the_caller_reads() -> None:
    """A failed source is named in the return value the caller reads.

    `gather_evidence` reads the return value, and an unreachable source must not reach the model as
    "nothing on file".
    """
    lists, failed, _skipped = asyncio.run(
        sweep_sources(
            [
                (s.name, s)
                for s in (
                    _Retriever("graph", 2),
                    _Retriever("lexical", 1, fails=True),
                    _Retriever("dense", 0),
                )
            ],
            "q",
            {},
        )
    )

    # Positions are unchanged: the failed leg still contributes an empty list where it stood.
    assert [len(chunks) for chunks in lists] == [2, 0, 0]
    # ...but only the leg that raised is named. `dense` ran fine and matched nothing.
    assert failed == ["lexical"]


def test_a_sweep_where_nothing_failed_names_nothing() -> None:
    """The control: an all-healthy sweep must not report a phantom degradation."""
    lists, failed, _skipped = asyncio.run(
        sweep_sources(
            [(s.name, s) for s in (_Retriever("graph", 1), _Retriever("dense", 0))], "q", {}
        )
    )

    assert [len(chunks) for chunks in lists] == [1, 0]
    assert failed == []


def test_a_declined_source_is_a_skip_not_a_failure_and_not_a_zero() -> None:
    """A declined source is a skip with a reason, not a failure and not a zero.

    "Found nothing", "could not ask" and "declined, and said why" stay distinct.
    """
    from chemclaw.retrieval.evidence import RetrieverSkip

    class _Declining:
        name = "sharedrive"

        async def retrieve(self, query: str, filters: dict[str, object]) -> list[EvidenceChunk]:
            raise RetrieverSkip("the sharedrive share requires an entitled actor")

    class _Healthy:
        name = "graph"

        async def retrieve(self, query: str, filters: dict[str, object]) -> list[EvidenceChunk]:
            return [EvidenceChunk(content="hit", source_note_id="n-1", retriever="graph")]

    lists, failed, skipped = asyncio.run(
        sweep_sources([("graph", _Healthy()), ("sharedrive", _Declining())], "q", {})
    )
    assert [len(chunks) for chunks in lists] == [1, 0]
    assert failed == [], "a decline is not an outage"
    assert skipped == {"sharedrive": "the sharedrive share requires an entitled actor"}
