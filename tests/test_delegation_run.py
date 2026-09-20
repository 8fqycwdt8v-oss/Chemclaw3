"""The delegation experiment's run half, held to what the comparator cannot see for itself.

`tests/test_delegation.py` covers `compare_arms`: given `ArmRun`s, the arithmetic. Nothing covered
where an `ArmRun` comes from, because until now nothing produced one. What is asserted here is
therefore the *observation* rather than the comparison — whether `delegated` is read off the record
that knows, whether an arm's own name or prompt could have leaked into it, and whether the three arm
profiles vary one thing or two.

The one property with no test here is the one only a run can have: that the pieces compose against a
front door. `make live-delegation` is that, it has been driven against `cli.mock_llm --catalogue
delegation`, and the run exits non-zero on purpose — a double supplies the decision to delegate, so
such a run is evidence about this runner and about nothing else.
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
    """One variable, which is the whole reason there are three files instead of one.

    A profile's `instructions:` **replace** the deployment's domain prose wholesale
    (`agent/chemclaw_agent.instructions_for`), so a baseline that supplies prose against a `default`
    treatment arm varies the delegation ask and the whole system prompt together — the instrument
    `D-2026-09-14-tools-were-never-the-variable` was written about, and on the token axis this
    experiment is mostly about, the prose is the larger of the two by an order of magnitude.

    Both directions, because either alone is satisfiable by doing nothing: the bodies must be
    byte-identical **and** the three asks must differ from one another. A single `Delegation:` per
    file is asserted too — two of them would make "the body" ambiguous and the split silently wrong.
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

    `BASELINE_ARM` cannot be built by taking `task` away — `SubAgentMiddleware` is in
    `create_deep_agent`'s `_REQUIRED_MIDDLEWARE` and an empty roster makes upstream re-insert its
    own — so this sentence is the whole treatment. The tool name is asked of
    `subagent_tool_names()` rather than written here: upstream spells it as a literal inside
    `_build_task_tool`, and a rename would otherwise leave a baseline profile asking the model not
    to call something that no longer exists, with this test still green.
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
    """`task` comes from the middleware and a handoff from `handoff_tool_name`, never from a string.

    Both names are minted elsewhere — upstream writes `task` inside `_build_task_tool`, and
    `agent/handoff.py` mints `transfer_to_<peer>` from a profile name with its hyphens replaced. A
    copy of either in `delegation_run` would be a third source that an upstream rename or a profile
    name containing a hyphen leaves silently stale, and silently stale here means an arm whose
    treatment cannot be recognised is reported as an arm that declined.
    """
    spawn = sorted(subagent_tool_names())[0]
    hand = handoff_tool_name("property-lookup")
    surface = {spawn, hand, "find_notes"}

    assert delegation_run.treatment_tools("helper", surface) == frozenset({spawn})
    assert delegation_run.treatment_tools("handoff", surface) == frozenset({hand})
    # The baseline's treatment is the union, and that matters only once a peer arm exists: the arms
    # share one front door, so a run including the peer arm has a handoff tool bound for *every*
    # arm, and a baseline that handed the conversation away is no more a baseline than one that
    # spawned a helper.
    assert delegation_run.treatment_tools("any", surface) == frozenset({spawn, hand})
    assert delegation_run.treatment_tools("helper", {"find_notes"}) == frozenset()
    with pytest.raises(ValueError, match="unknown treatment"):
        delegation_run.treatment_tools("delegation", surface)


def test_the_baselines_treatment_is_the_union_of_both_acts() -> None:
    """A property of the *table* as well as of the function, because the table is the risk.

    `treatment_tools` can be right while `ARMS` asks it the wrong question. A baseline declared with
    `treatment="helper"` would pass every assertion above and still let a run in which the baseline
    handed the conversation to a peer be compared as a clean baseline.
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
    """Intention-to-treat, at the point where a selection on the treatment would be easiest.

    `compare_arms` reports `undelegated` and `partially_delegated` as *compliance* and drops nothing
    for them, and that discipline is worth nothing if the runner never records the repeat in the
    first place. A non-delegating repeat that failed to become an `ArmRun` would arrive at the
    comparator as fewer repeats — `incomplete` — so the ITT reporting the comparator took three
    tries to get right would be defeated one layer up, invisibly.
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
    """Two failures that are not results, and not each other.

    `ungraded` is the *absence* of a verdict (`evals/live_judge.Verdict` records what conflating
    it with `unserved` cost: 65 of 190 probes mislabelled), and `VERDICT_SCORES` has no entry for
    it, so such a repeat cannot carry a quality. A session with no `turn_costs` row is the other:
    a turn that failed before billing legitimately records **zero**, so inventing zero for an
    unwritten row would put a fabricated cost into a comparison whose whole subject is cost.
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
        "an ungraded repeat has to be named as a hole; `VERDICT_SCORES` has no entry for it, so the "
        "alternative is a quality this harness invented"
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
    """Four arms, so a `NoComparableTask` is carried rather than raised.

    The comparator refuses an empty comparison on purpose — a delegation report over nothing reads
    as "no effect anywhere". With one arm per call that refusal is correct and total; across four
    arms it would mean an arm nobody could compare (a posture the front door was not started with,
    say) discards the three that ran.
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
    """`--compare-runs` is what assembles a partially-delegating pair at all.

    A `(task, arm)` whose repeats differ in whether they delegated cannot come out of one pass
    against a deterministic double, and against a real model it can arrive across two campaigns —
    so reading recorded runs back is the feature, not a convenience. A file that contributed nothing
    is refused before the comparator is asked, for `NoComparableTask`'s reason one step earlier.
    """
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    run = ArmRun(
        task_id="dl-01",
        arm="helper",
        quality=1.0,
        billed_tokens=1,
        wall_clock_seconds=1.0,
        delegated=True,
    )
    first.write_text(json.dumps([run.model_dump(), run.model_dump()]), encoding="utf-8")
    second.write_text(
        json.dumps([run.model_copy(update={"delegated": False}).model_dump()]), encoding="utf-8"
    )
    loaded = delegation_run.load_recorded_runs([first, second])
    assert [item.delegated for item in loaded] == [True, True, False]

    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="no recorded runs"):
        delegation_run.load_recorded_runs([empty])


def test_every_arms_scripted_behaviour_exists_and_every_behaviour_is_reached() -> None:
    """Both directions over the double's catalogue, which is where a dead entry hides.

    `tests/test_live_storm.py` holds the same property over the storm's catalogue and records why:
    six behaviours were declared and driven by nothing while the run reported "17/17 checks passed".
    That test reads the harness's *source* for each name, because a storm selector travels as a
    string inside a turn message and there is no symbol to resolve. Here there is: a behaviour is
    reached either because an `ArmSpec` names it or because another behaviour's rendered arguments
    carry its marker — and reading the rendered arguments rather than the file is what made this
    test bite on its first run, since the one marker this catalogue writes is built from a constant
    and the literal `[[d-helper-report]]` appears nowhere in the module.
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
    """A marker in an answer is read back as a note citation, which is this harness's worst signal.

    `evals/live._score_citations` parses `[[…]]` out of the answer and reports anything no tool
    returned as `uncited_note_ids` — "a citation that resolves to nothing is worse than no citation,
    because it reads as evidence". So a selector left in a behaviour's `text` would make every
    scripted answer cite a note that does not exist. The one marker this catalogue writes sits in a
    `task` **argument**, where it selects the helper's own behaviour and reaches no answer.
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
    """Catalogue *order*, which is a property nothing else would notice until a report read wrong.

    `MockLlm.select` returns the first declared behaviour whose marker appears anywhere in the
    serialized request, and one request carries two: the caller's second pass holds both its own
    question and the `task` arguments it wrote on the first. Declared the other way round, a
    delegating caller's final answer would be its helper's report — and since that report is prose
    rather than a verdict, every repeat of the treatment arm would come back `ungraded`.
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
    """On `question`, because the judge's own model call is a model call against the same gateway.

    `evals/live_judge._prompt` quotes `probe.question`, so a marker put only on the *message* would
    leave every grading request unmarked, the double would answer it as the catalogue's default, and
    the repeat would be a hole. The probe is copied rather than mutated: one corpus object is shared
    across every arm and every repeat.
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
    """The one flag here that could script a *result* rather than a double.

    `--mock-behaviour` exists so a compliance state a deterministic double cannot otherwise reach —
    a baseline that delegates, a treatment arm that does not — can be driven. Applied to a real
    model it would be a flag claiming to change what a model decided, so it is refused there rather
    than ignored: ignoring it would produce a report whose arms are not the arms the command asked
    for.
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
    """The one thing `audit_events` knows that a tool *name* alone does not.

    A gate — authorization, the plan gate, the dry-run guard — stops a call by raising above the
    tool body, so a `refused` row means the helper's graph was never entered and no helper was
    spawned. Every other outcome means the call was made: a `task` that entered and then failed is a
    delegation that happened, and under ITT it dilutes the effect toward zero, which is the
    conservative direction. Reading the name without the outcome would count a refusal as
    compliance — and on the baseline arm, as contamination.

    Against the real table, because the claim is about what the SQL selects.
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
