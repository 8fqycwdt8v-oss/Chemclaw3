"""The evidence sweep as a `Send` fan-out, and the balance it was supposed to fix (M10).

Two things are under test and they are not the same thing. The fan-out's own properties — order,
degradation, per-branch reporting — are asserted directly. The *reason* it exists is
`D-2026-08-01-a-cap-that-starves-a-source`, in which one retrieval leg contributed zero surviving
chunks while the sweep looked healthy in aggregate, and that is re-measured here against the
numbers the ADR recorded rather than asserted in the abstract.

`test_the_starved_source_measurement_rerun` is the one worth reading. It rebuilds the ADR's exact
mixed sweep — 45 graph hits, 8 lexical, 7 dense, against a 40-chunk cap — and reports what each
source contributes now. The ADR's own measurements were 38/0/2 under the flat union it removed and
40/0/0 with the score sort taken out; the test prints today's split so the number is in the record
rather than in a commit message.
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

    Drops the second half of `sweep_sources`' return — the names of the sources that raised — so
    the order and cap tests below keep asking exactly what they asked before that channel existed.
    The failure channel has its own tests; folding it in here would make every test depend on it.
    """
    lists, _failed = asyncio.run(sweep_sources([(s.name, s) for s in sources], "q", {}))
    return lists


def test_the_fan_in_is_in_source_order_not_completion_order() -> None:
    """The property `operator.add` does not give you — and why each branch carries an index.

    The first source is made much slower than the last, so completion order is the reverse of
    source order. Both merge modes downstream read these lists positionally — `reciprocal_rank_
    fusion` takes a note's representative chunk from the first list that found it, and the
    round-robin interleaves in list order — so a sweep whose order depended on which database
    answered first would return different evidence for the same question on different runs. In a
    chemist that is a reproducibility defect, not a nondeterminism nobody notices.
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
    """One dead retriever degrades the evidence; it does not fail the research question.

    The same trade the connector transport makes: losing a capability is a much smaller failure
    than losing the turn. The failed source contributes an empty list *in its own position*, so the
    sources after it keep their places and the merge downstream is unaffected.

    **The `[2, 0, 2]` here is about the merge, not about observability.** `sweep_sources` returns
    ranked hit-lists and nothing else, so a failed leg is an empty list here by construction —
    there is no third value a `list[EvidenceChunk]` could take. That is correct for this return
    type and was wrong as the *whole* record of the failure, which is what
    `test_a_failed_leg_and_an_empty_leg_report_differently` now covers on the channel that has
    somewhere to put it.
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
    """The two ways to contribute nothing, told apart on the channel a chemist watches.

    A retriever that raises degrades to an empty list, so for as long as a branch reported only a
    count the broken leg and the genuinely-quiet leg published the identical payload — the exact
    collapse this fan-out exists to undo, reproduced one level down. They are not the same finding:
    "the corpus has nothing on this" is an answer, "the index is down" is an outage, and a surface
    that renders them alike sends someone to fix the wrong thing.

    Asserted as a *difference* between two legs in one sweep rather than as a flag on one, because
    the defect was never "the flag was false" — it was that the two payloads were equal.
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
    """The across-turns half of the same distinction, and why the label had to be added.

    The chunk counter has always been labelled `{source}` and the failure counter carried no labels
    at all, so the two series could not be joined: "some source raised" and "the dense leg went
    dark" were two numbers with no way to decide whether they described one event or two. That is
    the correlation `D-2026-08-01-a-cap-that-starves-a-source` needed and did not have, and an
    unlabelled counter cannot supply it however long it is retained.
    """
    from chemclaw.core.metrics import METRICS

    _swept([_Retriever("graph", 1), _Retriever("dense", 1, fails=True)])
    rendered = METRICS.render()

    assert 'chemclaw_evidence_source_failures_total{source="dense"}' in rendered
    assert 'chemclaw_evidence_source_failures_total{source="graph"}' not in rendered


def test_no_sources_is_an_empty_sweep_and_not_an_error() -> None:
    """Every source disabled is a real deployment, not a misconfiguration to raise on."""
    assert _swept([]) == []


def test_each_branch_reports_what_it_contributed() -> None:
    """The point of the branch existing: a starved leg is visible while the sweep runs.

    In an aggregate hit-list a source returning nothing and a source nobody asked are the same
    observation. Here they are not — the branch reports zero, which is what
    `D-2026-08-01-a-cap-that-starves-a-source` needed and had no way to see.

    Read off the graph's own custom stream, which is how a surface receives it during a real turn.
    """
    sources = [_Retriever("graph", 3), _Retriever("lexical", 0), _Retriever("dense", 2)]
    reported = {item["evidence_source"]: item["chunks"] for item in _reports(sources)}
    assert reported == {"graph": 3, "lexical": 0, "dense": 2}


def test_the_starved_source_measurement_rerun(capsys: pytest.CaptureFixture[str]) -> None:
    """Re-measure `D-2026-08-01-a-cap-that-starves-a-source`, per branch (the M10 acceptance).

    The ADR's sweep, rebuilt: 45 graph hits at the notes' 0.8 confidence, 8 lexical at ts_rank
    0.02–0.09, 7 dense at cosine 0.60–0.85, against a 40-chunk cap. Its recorded measurements were
    **38 graph / 0 lexical / 2 dense** under the flat union that has since been removed, and
    **40 / 0 / 0** with the score sort taken out — either way the lexical leg contributed nothing
    an agent could read, which is the whole reason a deployment enables it.

    What is asserted is the property, not the exact split: **every source that had hits survives
    the cap**. Pinning precise per-source counts would freeze the round-robin's arithmetic against
    a corpus shape nobody promised, and the defect was never "the wrong ratio" — it was a zero.
    The measured split is printed so the number is in the record.
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
    """The end-to-end claim: a starved leg is visible to a *chemist*, not just to a counter.

    The counter makes a permanently-dark source alertable across turns; this makes one sweep's
    arithmetic visible while it happens, which is what `D-2026-08-01-a-cap-that-starves-a-source`
    lacked — that defect went unnoticed until someone counted by hand.

    Driven through the whole path rather than by calling the translator directly: the branch runs
    inside a tool, inside a `Send` branch of the agent's own model→tools edge, and the report has
    to cross both boundaries to arrive. Asserting on `_custom_event` alone would prove the mapping
    and skip the part that was in doubt.

    A third source is added that *raises*, because `failed` has the same two boundaries to cross
    and a flag set on the branch but dropped by the translator would be indistinguishable, from the
    chemist's side, from never having been set.
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
        lists, _failed = await sweep_sources([(s.name, s) for s in legs], query, {})
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
    """The stream event says a leg failed; the *return value* has to say so too.

    `test_a_failed_leg_and_an_empty_leg_report_differently` covers the surface watching the turn.
    This covers the caller — a different audience with a different problem: `gather_evidence`
    reads the return value, and while that carried hit-lists alone an unreachable source and a
    source with nothing to say were the same empty list. That is what let an outage be handed to the
    model under a docstring promising "nothing on file, never invented".
    """
    lists, failed = asyncio.run(
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
    lists, failed = asyncio.run(
        sweep_sources(
            [(s.name, s) for s in (_Retriever("graph", 1), _Retriever("dense", 0))], "q", {}
        )
    )

    assert [len(chunks) for chunks in lists] == [1, 0]
    assert failed == []
