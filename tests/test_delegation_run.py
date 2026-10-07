"""The delegation experiment's run half, held to what the comparator cannot see for itself.

`tests/test_delegation_eval.py` covers `compare_arms`; this covers where an `ArmRun` comes from:
whether `delegated` is read off the record that knows, whether an arm's name or prompt could leak
into it, and whether the arm profiles vary one thing. Composition against a front door is
`make live-delegation`'s job, and a run against the mock exits non-zero on purpose, since a
double supplies the decision to delegate.
"""

import asyncio
import json
from pathlib import Path

import pytest

from chemclaw.agent.audit import REFUSED, AuditEvent
from chemclaw.agent.chemclaw_agent import subagent_tool_names
from chemclaw.agent.handoff import handoff_tool_name
from chemclaw.agent.profile_discovery import _load
from chemclaw.cli import live_probes
from chemclaw.cli.delegation_behaviours import DELEGATION_BEHAVIOURS, HELPER_MARKER
from chemclaw.cli.mock_llm import MockLlm, catalogue
from chemclaw.evals import delegation_run
from chemclaw.evals.delegation import BASELINE_ARM, MINIMUM_REPEATS, ArmRun
from chemclaw.evals.live import ProbeOutcome
from chemclaw.evals.live_judge import Judgement
from chemclaw.evals.probe import Probe

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PROFILE_DIR = _REPO_ROOT / "data/evals/profiles"

#: The heading the one varying paragraph of every arm profile begins with. A literal here rather
#: than an import, deliberately: the three files' shared body is prose this test is checking, and
#: reading the separator out of the thing under test would let one edit satisfy both sides.
_ACT_HEADING = "Delegation:"

#: The arm profiles, which are the arms' only *structural* difference. Derived from `ARMS` rather
#: than listed, so a fifth arm pointing at a fourth profile file is covered the day it lands.
_ARM_PROFILES = sorted({spec.profile for spec in delegation_run.ARMS})


def _probe(probe_id: str = "dl-01") -> Probe:
    return Probe(
        id=probe_id,
        section=1,
        persona="lab_leader",
        bucket="A",
        question="sweep everything we hold on these couplings",
        direction="a page of recurring conditions with citations",
    )


def _outcome(probe_id: str = "dl-01", session_id: str = "s1", latency: float = 2.0) -> ProbeOutcome:
    return ProbeOutcome(
        probe_id=probe_id,
        section=1,
        persona="lab_leader",
        bucket="A",
        question="q",
        answer="a",
        answered=True,
        latency_seconds=latency,
        session_id=session_id,
    )


def _repeat(
    arm: str,
    session_id: str,
    verdict: str = "served",
    probe_id: str = "dl-01",
    repeat: int = 1,
) -> delegation_run.ArmRepeat:
    return delegation_run.ArmRepeat(
        arm=arm,
        repeat=repeat,
        outcome=_outcome(probe_id=probe_id, session_id=session_id),
        judgement=Judgement(probe_id=probe_id, verdict=verdict),  # type: ignore[arg-type]
    )


def test_the_three_arm_profiles_differ_only_in_their_delegation_ask() -> None:
    """The three arm profiles differ only in their delegation ask.

    A profile's `instructions:` replace the domain prose wholesale, so the bodies must be
    byte-identical **and** the asks must differ, or the whole prompt varies with the treatment
    (`D-2026-09-14-tools-were-never-the-variable`). Exactly one `Delegation:` per file, so the split
    is unambiguous.
    """
    asks: dict[str, str] = {}
    bodies: set[str] = set()
    for name in _ARM_PROFILES:
        profile = _load(_PROFILE_DIR / f"{name}.yaml")
        assert profile.instructions is not None, f"{name} supplies no instructions"
        assert profile.instructions.count(_ACT_HEADING) == 1, (
            f"{name} has {profile.instructions.count(_ACT_HEADING)} {_ACT_HEADING!r} paragraphs; "
            "with anything but one, 'the shared body' is ambiguous and this split is wrong"
        )
        body, _, ask = profile.instructions.partition(_ACT_HEADING)
        bodies.add(body)
        asks[name] = ask
    assert len(bodies) == 1, (
        f"the {len(_ARM_PROFILES)} arm profiles do not share one instruction body, so a delta this "
        "experiment reports varies the delegation ask and the system prompt together — which is "
        "D-2026-09-14-tools-were-never-the-variable, one measurement over"
    )
    assert len(set(asks.values())) == len(asks), (
        f"two arm profiles ask for the same thing: {asks}. Arms that differ in nothing are one arm "
        "under two labels, and the report would attribute the baseline's own numbers to a treatment"
    )


def test_the_baseline_profile_asks_the_model_not_to_call_task() -> None:
    """The behavioural arm is an *ask*, and the ask has to name the tool it is about.

    `task` cannot be removed (`SubAgentMiddleware` is required), so the sentence is the whole
    treatment. The name comes from `subagent_tool_names()` rather than a literal, so an upstream
    rename cannot leave the baseline asking about a tool that no longer exists.
    """
    spec = delegation_run.arm_by_name(BASELINE_ARM)
    profile = _load(_PROFILE_DIR / f"{spec.profile}.yaml")
    assert profile.instructions is not None
    _, _, ask = profile.instructions.partition(_ACT_HEADING)
    assert any(name in ask for name in subagent_tool_names()), (
        f"the baseline arm's ask names none of {sorted(subagent_tool_names())}, so it does not ask "
        "for the thing this arm is defined by"
    )
    assert "do not" in ask.lower(), f"the baseline's ask does not refuse anything: {ask!r}"


def test_the_treatment_is_read_off_the_producers_rather_than_a_literal() -> None:
    """The treatment is read off the producers rather than a literal.

    Upstream mints `task` in `_build_task_tool` and `agent/handoff.py` mints `transfer_to_<peer>`; a
    copy here would go stale on a rename or a hyphenated profile name, reporting an unrecognised
    treatment as a declined one.
    """
    spawn = sorted(subagent_tool_names())[0]
    hand = handoff_tool_name("property-lookup")
    surface = {spawn, hand, "find_notes"}

    assert delegation_run.treatment_tools("helper", surface) == frozenset({spawn})
    assert delegation_run.treatment_tools("handoff", surface) == frozenset({hand})
    # The baseline's treatment is the union: arms share one front door, so with a peer arm present a
    # baseline that handed off is no more a baseline than one that spawned a helper.
    assert delegation_run.treatment_tools("any", surface) == frozenset({spawn, hand})
    assert delegation_run.treatment_tools("helper", {"find_notes"}) == frozenset()
    with pytest.raises(ValueError, match="unknown treatment"):
        delegation_run.treatment_tools("delegation", surface)


def test_the_baselines_treatment_is_the_union_of_both_acts() -> None:
    """The baseline's treatment is the union of both acts, checked on the `ARMS` table too.

    `treatment_tools` can be right while `ARMS` asks it the wrong question (`treatment="helper"`).
    """
    assert delegation_run.arm_by_name(BASELINE_ARM).treatment == "any", (
        "the baseline's treatment must be the union of both acts, or a baseline that handed the "
        "conversation to a peer is compared as though it had not delegated"
    )
    for spec in delegation_run.ARMS:
        assert spec.treatment in delegation_run.TREATMENTS
        if spec.arm != BASELINE_ARM:
            assert spec.treatment != "any", (
                f"arm {spec.arm!r} claims both acts as its treatment; an arm under test has "
                "exactly one, or `delegated` stops saying whether it did what its name claims"
            )


def test_a_repeat_that_did_not_delegate_is_recorded_rather_than_dropped() -> None:
    """A repeat that did not delegate is recorded rather than dropped.

    Otherwise it would reach the comparator as a missing repeat (`incomplete`), defeating
    intention-to-treat one layer up.
    """
    result = delegation_run.assemble_runs(
        [_repeat("helper", "s1", repeat=1), _repeat("helper", "s2", repeat=2)],
        ran={"s1": frozenset({"task"}), "s2": frozenset({"find_notes"})},
        billed={"s1": 10_000, "s2": 2_000},
        treatments={"helper": "helper"},
    )
    assert [run.delegated for run in result.runs] == [True, False], (
        "a repeat that did not delegate has to be recorded with `delegated=False`, not dropped: "
        "dropping it reaches the comparator as a missing repeat and reads as a hole in the data"
    )
    assert not result.ungraded and not result.unbilled
    assert [run.billed_tokens for run in result.runs] == [10_000, 2_000]


def test_a_repeat_with_no_verdict_and_one_with_no_cost_are_different_named_holes() -> None:
    """A repeat with no verdict and one with no cost are different, named holes.

    `ungraded` is the absence of a verdict and has no score. A session with no `turn_costs` row is
    not zero cost: a turn that failed before billing records zero legitimately, so inventing zero
    would fabricate a cost.
    """
    result = delegation_run.assemble_runs(
        [
            # s1 answered and was billed, and the judge could not grade it.
            _repeat("helper", "s1", verdict="ungraded"),
            # s2 was graded and the ledger holds no row for it.
            _repeat("helper", "s2", repeat=2),
            # s3 was graded and billed **zero**, which is a real cost and must survive.
            _repeat("helper", "s3", repeat=3),
        ],
        ran={},
        billed={"s1": 9_000, "s3": 0},
        treatments={"helper": "helper"},
    )
    assert result.ungraded == ["helper/dl-01#1"], (
        "an ungraded repeat has to be named as a hole; `VERDICT_SCORES` has no entry for it, so "
        "the alternative is a quality this harness invented"
    )
    assert result.unbilled == ["helper/dl-01#2"], (
        "a session the ledger holds no row for has to be named as a hole rather than read as zero: "
        "a fabricated zero would go straight into the token ratio, and cost is this comparison's "
        "whole subject"
    )
    assert [run.billed_tokens for run in result.runs] == [0], (
        "the one repeat that survives is the one billed zero — a booked zero is a real cost, and "
        "collapsing it with an unwritten row loses the distinction in the direction that fabricates"
    )


def test_one_arm_that_cannot_be_reported_on_does_not_cost_the_others_their_report() -> None:
    """One arm that cannot be reported on does not cost the others their report.

    The comparator refuses an empty comparison on purpose; across four arms that refusal is carried
    per arm rather than raised, so one uncomparable arm does not discard the rest.
    """
    runs = [
        ArmRun(
            task_id="dl-01",
            arm=arm,
            quality=1.0,
            billed_tokens=tokens,
            wall_clock_seconds=1.0,
            delegated=arm != BASELINE_ARM,
        )
        for arm, tokens in ((BASELINE_ARM, 10_000), ("helper", 8_000))
        for _ in range(MINIMUM_REPEATS)
    ]
    reports, refused = delegation_run.compare_every_arm(
        runs, [BASELINE_ARM, "helper", "peer"], MINIMUM_REPEATS
    )
    assert set(reports) == {"helper"}
    assert set(refused) == {"peer"}, (
        "an arm with no runs must be reported as unreportable beside the arms that ran, not raise "
        "past the report they paid for"
    )
    assert reports["helper"].median_token_ratio == pytest.approx(0.8)


def test_recorded_runs_aggregate_across_passes_and_an_empty_file_is_refused(
    tmp_path: Path,
) -> None:
    """`--compare-runs` aggregates recorded runs across passes; an empty file is refused.

    A partially delegating `(task, arm)` cannot come out of one deterministic pass, and against a
    real model may span campaigns. A file that contributed nothing is refused before the comparator.
    """
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"

    def recorded(*, delegated: bool) -> ArmRun:
        """Built through the constructor, because `load_recorded_runs` validates what it reads.

        `model_copy(update=...)` would assign the one observation this module records past the
        boundary production crosses (`tests/test_model_copy_fixtures.py`).
        """
        return ArmRun(
            task_id="dl-01",
            arm="helper",
            quality=1.0,
            billed_tokens=1,
            wall_clock_seconds=1.0,
            delegated=delegated,
        )

    delegating = recorded(delegated=True)
    first.write_text(
        json.dumps([delegating.model_dump(), delegating.model_dump()]), encoding="utf-8"
    )
    second.write_text(json.dumps([recorded(delegated=False).model_dump()]), encoding="utf-8")
    loaded = delegation_run.load_recorded_runs([first, second])
    assert [item.delegated for item in loaded] == [True, True, False]

    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="no recorded runs"):
        delegation_run.load_recorded_runs([empty])


def test_every_arms_scripted_behaviour_exists_and_every_behaviour_is_reached() -> None:
    """Both directions over the double's catalogue, which is where a dead entry hides.

    Every scripted behaviour must be reached, either named by an `ArmSpec` or carried as a marker in
    another behaviour's *rendered* arguments (the marker is built from a constant, so reading the
    file would miss it).
    """
    declared = {behaviour.name for behaviour in DELEGATION_BEHAVIOURS}
    wanted = {spec.mock_behaviour for spec in delegation_run.ARMS}
    assert wanted <= declared, (
        f"{sorted(wanted - declared)} is named by an arm and declared nowhere"
    )
    written = json.dumps(
        [call.arguments for behaviour in DELEGATION_BEHAVIOURS for call in behaviour.calls]
    )
    unreached = sorted(declared - wanted - {name for name in declared if f"[[{name}]]" in written})
    assert not unreached, (
        f"{unreached} is declared and reached by nothing: neither an arm's `mock_behaviour` nor a "
        "marker any behaviour writes. A catalogue entry no run reaches is coverage the report "
        "cannot claim and a reader will assume"
    )


def test_no_scripted_answer_carries_a_behaviour_marker() -> None:
    """No scripted answer carries a behaviour marker.

    `evals/live._score_citations` reads `[[…]]` in an answer as a citation and reports unresolved
    ones, so a marker in `text` would fabricate a dangling citation. The one marker sits in a `task`
    argument, which reaches no answer.
    """
    names = {behaviour.name for behaviour in DELEGATION_BEHAVIOURS}
    for behaviour in DELEGATION_BEHAVIOURS:
        for name in names:
            assert f"[[{name}]]" not in behaviour.text, (
                f"behaviour {behaviour.name!r} writes the marker [[{name}]] into its answer, which "
                "`_score_citations` will report as an answer citing a note no tool returned"
            )
    assert any(
        f"[[{HELPER_MARKER}]]" in json.dumps(call.arguments)
        for behaviour in DELEGATION_BEHAVIOURS
        for call in behaviour.calls
    ), (
        "no behaviour selects the helper's own script, so a helper would answer as the "
        "catalogue's default"
    )


def test_a_caller_carrying_both_markers_answers_as_itself_rather_than_as_its_helper() -> None:
    """A caller carrying both markers answers as itself rather than as its helper.

    `MockLlm.select` returns the first declared behaviour whose marker appears, and the caller's
    second pass carries both; in the other order its final answer would be the helper's report and
    every treatment repeat would be `ungraded`.
    """
    mock = MockLlm(catalogue("delegation"))
    payload = {
        "messages": [
            {"role": "user", "content": "[[d-delegates]] sweep the corpus"},
            {"role": "assistant", "content": f"calling task with [[{HELPER_MARKER}]]"},
        ]
    }
    assert mock.select(payload).name == "d-delegates", (
        "a caller holding both markers selected its helper's script; declare `d-delegates` before "
        f"{HELPER_MARKER!r}"
    )


def test_the_marker_lands_where_the_judge_will_also_see_it() -> None:
    """The marker lands on `question`, where the judge will also see it.

    `evals/live_judge._prompt` quotes `probe.question`; a marker only on the message would leave
    grading requests unmarked. The probe is copied, since one corpus object is shared across arms
    and repeats.
    """
    probe = _probe()
    marked = live_probes._marked(probe, "d-delegates")
    assert marked.question.startswith("[[d-delegates]] ")
    assert probe.question == "sweep everything we hold on these couplings", (
        "the corpus probe was mutated in place, so every later arm would carry this arm's marker"
    )
    from chemclaw.evals.live_judge import _prompt

    assert "[[d-delegates]]" in _prompt(marked, _outcome())


def test_scripting_the_double_is_refused_against_a_real_gateway() -> None:
    """Scripting the double is refused against a real gateway.

    `--mock-behaviour` drives compliance states a deterministic double cannot otherwise reach; on a
    real model it would claim to change what the model decided, so it is refused, not ignored.
    """
    assert live_probes._mock_behaviour_overrides(["helper=d-no-helper"], mock=True) == {
        "helper": "d-no-helper"
    }
    with pytest.raises(SystemExit, match="only applies to the scripted mock"):
        live_probes._mock_behaviour_overrides(["helper=d-no-helper"], mock=False)
    with pytest.raises(SystemExit, match="wants arm=behaviour"):
        live_probes._mock_behaviour_overrides(["helper"], mock=True)
    # Empty against a real gateway is not an override and must not refuse a run that asked for none.
    assert live_probes._mock_behaviour_overrides([], mock=False) == {}


def test_a_refused_task_is_not_a_delegation() -> None:
    """A refused `task` is not a delegation.

    A gate stops a call above the tool body, so a `refused` row means no helper ran; any other
    outcome is a delegation that happened. Reading the name without the outcome would count a
    refusal as compliance (or as contamination on the baseline). Against the real table, since the
    claim is about the SQL.
    """
    from tests.pg import migrated_db_or_skip

    async def drive() -> dict[str, frozenset[str]]:
        await migrated_db_or_skip()
        from chemclaw.agent.audit_store import PostgresAuditSink

        sink = PostgresAuditSink()
        for session_id, outcome in (("dg-ok", "ok"), ("dg-refused", REFUSED), ("dg-err", "error")):
            await sink.record(
                AuditEvent(
                    correlation_id=f"c-{session_id}",
                    session_id=session_id,
                    actor="u-1",
                    tool="task",
                    arguments="{}",
                    outcome=outcome,
                    detail="",
                    latency_ms=1.0,
                )
            )
        await sink.flush()
        return await delegation_run.tools_that_ran(["dg-ok", "dg-refused", "dg-err"])

    ran = asyncio.run(drive())
    assert ran.get("dg-ok") == frozenset({"task"})
    assert ran.get("dg-err") == frozenset({"task"}), (
        "a `task` that entered the helper's graph and then failed is a delegation that happened"
    )
    assert "dg-refused" not in ran, (
        "a call a gate refused never reached the tool body, so no helper was spawned — counting it "
        "would report a held gate as compliance, and on the baseline arm as contamination"
    )


def test_the_audit_read_and_the_ledger_read_each_use_their_own_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`audit_events` is on `postgres_dsn`; `turn_costs` is on the session store's DSN.

    Reading the audit trail off the session DSN would find no rows on a split deployment and report
    every repeat undelegated. Driven with the DSNs apart and the connection recorded, not opened.
    """
    from contextlib import asynccontextmanager
    from typing import Any

    from chemclaw.core import db
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "postgres_dsn", "postgresql://audit")
    monkeypatch.setattr(settings, "session_store_dsn", "postgresql://sessions")
    opened: list[str] = []

    class _Cursor:
        async def __aenter__(self) -> "_Cursor":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, *_: object) -> None:
            return None

        async def fetchall(self) -> list[Any]:
            return []

    class _Conn:
        def cursor(self) -> _Cursor:
            return _Cursor()

    @asynccontextmanager
    async def fake_connection(dsn: str, *_: object, **__: object) -> Any:
        opened.append(dsn)
        yield _Conn()

    monkeypatch.setattr(db, "connection", fake_connection)
    asyncio.run(delegation_run.tools_that_ran(["s"]))
    asyncio.run(delegation_run.billed_by_session(["s"]))
    assert opened == ["postgresql://audit", "postgresql://sessions"]
