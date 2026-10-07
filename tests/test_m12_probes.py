"""The M12 re-validation harnesses and their probe corpus, where they are pure.

What the suites measure needs a live stack (`make live-plan-gate`, `make live-degradation`). This
file covers what can be wrong silently:

* the scoring, a pure function over a recorded run, so a scoring bug is fixable without
  re-running the measurement;
* the wire reading, through `httpx.MockTransport` with exact SSE frames, so a plan-gate refusal is
  told apart from a broken tool and a late `capability_degraded` frame is caught;
* the corpus as a declaration: duplicate ids are fatal, unknown keys are rejected, and every
  `expects_tools` name exists on the agent surface.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import yaml

from chemclaw.agent.chemclaw_agent import available_tool_names
from chemclaw.agent.plan_gate import PLAN_GATE_REASON as GATE_REASON
from chemclaw.agent.plan_gate import plan_approval_refusal
from chemclaw.api.events import ToolFailedEvent
from chemclaw.cli.live_probes import _M12_SUITES, _findings_report, _m12_probes
from chemclaw.core.config import settings
from chemclaw.evals.live import (
    PLAN_GATE_REASON,
    Finding,
    PlanGateRun,
    PlanSnapshot,
    ProbeOutcome,
    _plan_gate_findings,
    degradation_findings,
    load_probes,
    run_plan_gate_probe,
    run_probe,
    run_turn,
)
from chemclaw.evals.probe import Probe, ProbeSet, Turn

M12_DIR = Path(__file__).resolve().parent.parent / "data" / "evals" / "probes" / "m12"

# Every state-changing tool the plan-gate suite is handed in these tests. A literal pair rather than
# the live `side_effecting_tools()`, so a test asserting the *scoring* does not fail the day the
# gated surface grows a tool — the corpus test below is what checks the suite against the real set.
GATED = frozenset({"compute_reaction_energy", "record_knowledge_note"})


def _probe(**overrides: object) -> Probe:
    """A minimal valid probe; overrides name only what a case actually varies."""
    payload: dict[str, object] = {
        "id": "m-01",
        "section": 1,
        "persona": "lab_technician",
        "bucket": "A",
        "question": "compute it and write it down",
        "direction": "runs the calculation",
    }
    payload.update(overrides)
    return Probe.model_validate(payload)


SSE_HEADERS = {"content-type": "text/event-stream"}
"""The header the front door sets and without which the reader decodes nothing — see
`tests/test_live_probes.SSE_HEADERS`."""


def _sse(*events: dict[str, object]) -> bytes:
    """Exactly the wire shape the front door emits: one `data:` line per event."""
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


def _run_one(probe: Probe, *events: dict[str, object]) -> ProbeOutcome:
    """Drive one turn against a scripted event stream."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": "s1"})
        return httpx.Response(200, content=_sse(*events), headers=SSE_HEADERS)

    async def go() -> ProbeOutcome:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://front-door"
        ) as client:
            return await run_turn(client, probe, message=probe.question)

    return asyncio.run(go())


# --------------------------------------------------------------------------- the shared turn runner


def test_a_scripted_probe_is_refused_by_the_single_question_runner() -> None:
    """A scripted probe is refused by the single-question runner.

    `run_probe` would ask only the first question and report the probe answered while the turns
    carrying the assertion never happened.
    """

    async def go() -> None:
        async with httpx.AsyncClient(base_url="http://front-door") as client:
            await run_probe(client, _probe(follow_ups=[{"message": "go ahead"}]))

    with pytest.raises(ValueError, match="scripted"):
        asyncio.run(go())


def test_a_plan_refusal_is_not_counted_as_a_broken_tool() -> None:
    """A plan refusal is not counted as a broken tool.

    Both arrive as `tool_failed` (`announce_tool_failures` sees `PlanNotApprovedError` raw); folding
    them together would report the gate holding as a fault.
    """
    outcome = _run_one(
        _probe(),
        {"type": "tool_call", "tool": "compute_reaction_energy", "arguments": "{}"},
        _refused("compute_reaction_energy"),
        {"type": "tool_failed", "tool": "gather_evidence", "message": "the index is unreachable"},
        {"type": "answer", "text": "I need your approval before I can run that."},
    )
    assert outcome.plan_refusals == ["compute_reaction_energy"]
    assert outcome.tools_failed == ["gather_evidence"]


def test_the_classifier_reads_the_discriminator_and_not_the_refusal_prose() -> None:
    """The classifier reads the `reason` discriminator, not the refusal prose.

    Rewording the refusal must not move the tally, and dropping the field must. An unstamped
    `tool_failed` is an ordinary failure whatever it says, so an older front door under-credits the
    gate, which is the safe direction to be wrong.
    """
    reworded = _run_one(
        _probe(),
        {
            "type": "tool_failed",
            "tool": "record_knowledge_note",
            "message": "Sign-off on the plan is outstanding, so this step was held.",
            "reason": PLAN_GATE_REASON,
        },
        {"type": "answer", "text": "I need your approval before I write that up."},
    )
    assert reworded.plan_refusals == ["record_knowledge_note"]
    assert reworded.tools_failed == []

    unstamped = _run_one(
        _probe(),
        {
            "type": "tool_failed",
            "tool": "record_knowledge_note",
            "message": str(plan_approval_refusal("record_knowledge_note")),
        },
        {"type": "answer", "text": "I need your approval before I write that up."},
    )
    assert unstamped.plan_refusals == []
    assert unstamped.tools_failed == ["record_knowledge_note"]


def test_every_gate_is_scored_as_the_control_working_not_as_a_broken_tool() -> None:
    """Every gate is scored as the control working, not as a broken tool.

    The gates are read from `core/turn_signals.RefusalReason` rather than restated, so a new one
    fails here instead of being scored as a fault. `plan_gate` keeps its own list because
    `_plan_gate_findings` asks what the plan gate held.
    """
    from typing import get_args

    from chemclaw.core.turn_signals import RefusalReason

    reasons = [reason for reason in get_args(RefusalReason) if reason != PLAN_GATE_REASON]
    assert reasons, "RefusalReason names only the plan gate — has the union changed?"

    outcome = _run_one(
        _probe(),
        *[
            {
                "type": "tool_failed",
                "tool": f"tool_{reason}",
                "message": "held",
                "reason": reason,
            }
            for reason in reasons
        ],
        {"type": "tool_failed", "tool": "gather_evidence", "message": "the index is unreachable"},
        {"type": "answer", "text": "Some steps were held."},
    )
    assert outcome.tool_refusals == [f"tool_{reason}" for reason in reasons]
    assert outcome.tools_failed == ["gather_evidence"], (
        f"a gate refusal was scored as a broken tool: {outcome.tools_failed}"
    )
    assert outcome.plan_refusals == []


def test_a_turn_held_entirely_by_gates_is_not_reported_as_having_failed_loudly() -> None:
    """A turn held entirely by gates is not reported as having failed loudly.

    `failed_loudly` reads `tools_failed`, so a refusal there would make a correctly gated turn the
    harness's loudest finding.
    """
    outcome = _run_one(
        _probe(),
        {
            "type": "tool_failed",
            "tool": "record_knowledge_note",
            "message": "held: this session is a dry run",
            "reason": "dry_run",
        },
        {"type": "answer", "text": "Nothing was written — this is a dry run."},
    )
    assert not outcome.failed_loudly
    assert outcome.tool_refusals == ["record_knowledge_note"]


def test_the_plan_gate_reason_is_the_one_the_wire_actually_carries() -> None:
    """`PLAN_GATE_REASON` matches the value the wire actually carries.

    It is a literal so loading a run does not build the agent layer, which is safe only while this
    checks it against the gate's constant and the `ToolFailedEvent.reason` `Literal`. A mismatch
    would flip both plan-gate findings at once.
    """
    assert PLAN_GATE_REASON == GATE_REASON
    assert ToolFailedEvent(tool="t", message="m", reason=PLAN_GATE_REASON).reason == GATE_REASON


def _plan_gate_transport(
    turns: list[list[dict[str, object]]], plans: list[dict[str, object]], decision_status: int = 204
) -> httpx.MockTransport:
    """A front door that serves a scripted conversation: N turns, N plans, one decision.

    The plan route serves from a queue, so a test can script an approved plan followed by a
    different unapproved one without reimplementing the gate.
    """
    remaining_turns = list(turns)
    remaining_plans = list(plans)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sessions":
            return httpx.Response(200, json={"session_id": "s1"})
        if path.endswith("/plan"):
            return httpx.Response(200, json=remaining_plans.pop(0))
        if path.endswith("/plan/decision"):
            return httpx.Response(decision_status)
        return httpx.Response(200, content=_sse(*remaining_turns.pop(0)), headers=SSE_HEADERS)

    return httpx.MockTransport(handler)


def _plan(hash_: str, items: list[str], *, approved: bool = False) -> dict[str, object]:
    """One `GET /sessions/{id}/plan` body."""
    return {
        "session_id": "s1",
        "plan_hash": hash_,
        "plan": items,
        "mode": "execute" if approved else "plan",
        "approved": approved,
        "decided_by": "chemist@lab" if approved else None,
    }


def _refused(tool: str) -> dict[str, object]:
    """A `tool_failed` frame as the front door emits one for a plan-gate refusal.

    Carries both the refusal sentence (built from `plan_approval_refusal`, not hand-typed) and the
    `reason` discriminator, which is all the classifier reads.
    """
    return {
        "type": "tool_failed",
        "tool": tool,
        "message": str(plan_approval_refusal(tool)),
        "reason": PLAN_GATE_REASON,
    }


def _healthy_run() -> PlanGateRun:
    """The conversation the gate is supposed to produce, driven over the wire.

    Three turns: a refused write, an approved one that runs, and a changed plan that is re-gated.
    The plan route is read after each turn, so `plans[1]` is the approved plan the third turn
    departs from.
    """
    probe = _probe(
        expects_tools=["compute_reaction_energy"],
        follow_ups=[
            Turn(message="approved — go ahead", before="approve_plan").model_dump(),
            Turn(message="different question entirely").model_dump(),
        ],
    )
    transport = _plan_gate_transport(
        turns=[
            [
                {"type": "tool_call", "tool": "compute_reaction_energy", "arguments": "{}"},
                _refused("compute_reaction_energy"),
                {"type": "answer", "text": "I need your approval first."},
            ],
            [
                {"type": "tool_call", "tool": "compute_reaction_energy", "arguments": "{}"},
                {"type": "tool_result", "tool": "compute_reaction_energy", "preview": "-32.6"},
                {"type": "answer", "text": "Done: -32.6 kcal/mol."},
            ],
            [
                {"type": "tool_call", "tool": "record_knowledge_note", "arguments": "{}"},
                _refused("record_knowledge_note"),
                {"type": "answer", "text": "That is a new plan; it needs its own approval."},
            ],
        ],
        # Read after turn 1 (unapproved), before the decision, after turn 2 (approved), and after
        # turn 3 (a new plan, therefore a new hash and no decision against it).
        plans=[
            _plan("hash-a", ["compute it", "write it up"]),
            _plan("hash-a", ["compute it", "write it up"]),
            _plan("hash-a", ["compute it", "write it up"], approved=True),
            _plan("hash-b", ["look up palladium removal", "propose a note"]),
        ],
    )

    async def go() -> PlanGateRun:
        async with httpx.AsyncClient(transport=transport, base_url="http://front-door") as client:
            return await run_plan_gate_probe(client, probe, gated_tools=GATED)

    return asyncio.run(go())


def test_the_whole_plan_gate_conversation_passes_when_the_gate_holds() -> None:
    """The reference run: refuse, approve, execute, re-gate — four findings, all green."""
    run = _healthy_run()
    assert [finding.check for finding in run.findings] == [
        "a plan a human can decide on",
        "an unapproved state-changing call is refused",
        "the decision was accepted",
        "the approved plan executes",
        "a changed plan is re-gated (DARK-1)",
    ]
    assert all(finding.ok for finding in run.findings), [
        (f.check, f.observed) for f in run.findings if not f.ok
    ]
    assert run.decision_statuses == [204]
    assert len(run.turns) == 3


def test_dark_1_itself_fails_the_suite_when_the_approval_outlives_its_plan() -> None:
    """An approval outliving its plan fails the suite: the changed plan's write must not run.

    Scored from a recorded run so the assertion holds on a diff.
    """
    probe = _probe(
        follow_ups=[
            Turn(message="approved", before="approve_plan").model_dump(),
            Turn(message="something else entirely").model_dump(),
        ]
    )
    run = PlanGateRun(
        probe_id=probe.id,
        session_id="s1",
        turns=[
            ProbeOutcome(
                probe_id=probe.id,
                section=1,
                persona="lab_technician",
                bucket="A",
                question=probe.question,
                tools_called=["compute_reaction_energy"],
                plan_refusals=["compute_reaction_energy"],
            ),
            ProbeOutcome(
                probe_id=probe.id,
                section=1,
                persona="lab_technician",
                bucket="A",
                question="approved",
                tools_called=["compute_reaction_energy"],
            ),
            # The defect: a different plan, no new decision, and the write ran anyway.
            ProbeOutcome(
                probe_id=probe.id,
                section=1,
                persona="lab_technician",
                bucket="A",
                question="something else entirely",
                tools_called=["record_knowledge_note"],
            ),
        ],
        plans=[
            PlanSnapshot(plan_hash="hash-a", plan=["a", "b"]),
            PlanSnapshot(plan_hash="hash-a", plan=["a", "b"], approved=True),
            PlanSnapshot(plan_hash="hash-b", plan=["c"], approved=True),
        ],
        decision_statuses=[204],
    )
    findings = {f.check: f for f in _plan_gate_findings(probe, run, GATED)}
    dark = findings["a changed plan is re-gated (DARK-1)"]
    assert dark.ok is False
    assert "record_knowledge_note" in dark.observed


def test_a_turn_that_never_attempted_a_write_is_a_miss_not_a_pass() -> None:
    """A turn that never attempted a write is a miss, not a pass.

    "No state-changing call ran before approval" is also true with no gate at all, or when the model
    never planned a write.
    """
    probe = _probe(follow_ups=[Turn(message="go", before="approve_plan").model_dump()])
    run = PlanGateRun(
        probe_id=probe.id,
        turns=[
            ProbeOutcome(
                probe_id=probe.id,
                section=1,
                persona="lab_technician",
                bucket="A",
                question=probe.question,
                tools_called=["gather_evidence"],
            ),
            ProbeOutcome(
                probe_id=probe.id,
                section=1,
                persona="lab_technician",
                bucket="A",
                question="go",
                tools_called=["compute_reaction_energy"],
            ),
        ],
        plans=[PlanSnapshot(plan_hash="hash-a", plan=["a"]), PlanSnapshot(plan_hash="hash-a")],
        decision_statuses=[204],
    )
    findings = {f.check: f for f in _plan_gate_findings(probe, run, GATED)}
    assert findings["an unapproved state-changing call is refused"].ok is False


def test_an_announced_but_refused_call_does_not_count_as_having_executed() -> None:
    """An announced but refused call does not count as having executed.

    A refused call still announces itself, so reading `tools_called` alone would report the defect
    and the fix identically.
    """
    probe = _probe(follow_ups=[Turn(message="go", before="approve_plan").model_dump()])
    run = PlanGateRun(
        probe_id=probe.id,
        turns=[
            ProbeOutcome(
                probe_id=probe.id,
                section=1,
                persona="lab_technician",
                bucket="A",
                question=probe.question,
                tools_called=["compute_reaction_energy"],
                plan_refusals=["compute_reaction_energy"],
            ),
            ProbeOutcome(
                probe_id=probe.id,
                section=1,
                persona="lab_technician",
                bucket="A",
                question="go",
                tools_called=["compute_reaction_energy"],
                plan_refusals=["compute_reaction_energy"],
            ),
        ],
        plans=[PlanSnapshot(plan_hash="hash-a", plan=["a"]), PlanSnapshot(plan_hash="hash-a")],
        decision_statuses=[204],
    )
    findings = {f.check: f for f in _plan_gate_findings(probe, run, GATED)}
    assert findings["the approved plan executes"].ok is False


def test_a_write_another_gate_held_is_not_scored_as_a_write_that_ran() -> None:
    """A write another gate held is not scored as a write that ran.

    The subtraction covers every refusal list (`dry_run`, `undeclared_write`, `repeat`, `authz`),
    not only `plan_refusals`; otherwise a held write would score the approval as proven and
    manufacture a bypass report.
    """
    probe = _probe(
        follow_ups=[
            Turn(message="approved", before="approve_plan").model_dump(),
            Turn(message="something else entirely").model_dump(),
        ]
    )

    def _held(question: str) -> ProbeOutcome:
        """A turn whose gated write was announced and refused by the authz gate."""
        return ProbeOutcome(
            probe_id=probe.id,
            section=1,
            persona="lab_technician",
            bucket="A",
            question=question,
            tools_called=["record_knowledge_note"],
            tool_refusals=["record_knowledge_note"],
        )

    run = PlanGateRun(
        probe_id=probe.id,
        session_id="s1",
        turns=[
            ProbeOutcome(
                probe_id=probe.id,
                section=1,
                persona="lab_technician",
                bucket="A",
                question=probe.question,
                tools_called=["record_knowledge_note"],
                plan_refusals=["record_knowledge_note"],
            ),
            _held("approved"),
            _held("something else entirely"),
        ],
        plans=[
            PlanSnapshot(plan_hash="hash-a", plan=["a"]),
            PlanSnapshot(plan_hash="hash-a", plan=["a"], approved=True),
            PlanSnapshot(plan_hash="hash-b", plan=["b"]),
        ],
        decision_statuses=[204],
    )
    findings = {f.check: f for f in _plan_gate_findings(probe, run, GATED)}

    executes = findings["the approved plan executes"]
    assert executes.ok is False, f"a write nothing performed was scored green: {executes.observed}"
    # …and the report says *why* it is false, so "the model never planned a write" and "another
    # gate held the write" are not the same empty list to whoever reads the run.
    assert "held by another gate ['record_knowledge_note']" in executes.observed

    dark = findings["a changed plan is re-gated (DARK-1)"]
    assert dark.ok is True, f"a bypass that did not happen was reported: {dark.observed}"
    assert "another gate held ['record_knowledge_note']" in dark.observed


def test_a_script_that_never_changes_the_plan_cannot_report_dark_1_as_passed() -> None:
    """Two turns test the approval; only a third tests the *binding*.

    Reported as a failed check rather than an omitted one, because a suite that quietly drops the
    assertion it is named for is the failure mode this whole file is arranged against.
    """
    probe = _probe(follow_ups=[Turn(message="go", before="approve_plan").model_dump()])
    run = PlanGateRun(
        probe_id=probe.id,
        turns=[
            ProbeOutcome(
                probe_id=probe.id,
                section=1,
                persona="lab_technician",
                bucket="A",
                question=probe.question,
                tools_called=["compute_reaction_energy"],
                plan_refusals=["compute_reaction_energy"],
            ),
            ProbeOutcome(
                probe_id=probe.id,
                section=1,
                persona="lab_technician",
                bucket="A",
                question="go",
                tools_called=["compute_reaction_energy"],
            ),
        ],
        plans=[PlanSnapshot(plan_hash="hash-a", plan=["a"]), PlanSnapshot(plan_hash="hash-a")],
        decision_statuses=[204],
    )
    findings = {f.check: f for f in _plan_gate_findings(probe, run, GATED)}
    assert findings["a changed plan is re-gated (DARK-1)"].ok is False


def test_a_rejected_decision_is_reported_rather_than_assumed() -> None:
    """A 409 means the plan changed between being read and being approved.

    That is the binding working, and it makes everything after it unmeasurable — so the suite says
    so instead of grading the turns that follow as though an approval existed.
    """
    probe = _probe(follow_ups=[Turn(message="go", before="approve_plan").model_dump()])
    run = PlanGateRun(
        probe_id=probe.id,
        turns=[
            ProbeOutcome(
                probe_id=probe.id,
                section=1,
                persona="lab_technician",
                bucket="A",
                question=probe.question,
                plan_refusals=["compute_reaction_energy"],
                tools_called=["compute_reaction_energy"],
            ),
            ProbeOutcome(
                probe_id=probe.id,
                section=1,
                persona="lab_technician",
                bucket="A",
                question="go",
            ),
        ],
        plans=[PlanSnapshot(plan_hash="hash-a", plan=["a"]), PlanSnapshot(plan_hash="hash-a")],
        decision_statuses=[409],
    )
    findings = {f.check: f for f in _plan_gate_findings(probe, run, GATED)}
    assert findings["the decision was accepted"].ok is False
    assert "409" in findings["the decision was accepted"].observed


def test_a_probe_scripting_no_approval_fails_before_it_grades_anything() -> None:
    """A plan-gate probe with no `approve_plan` turn exercises nothing and must say so."""
    probe = _probe(follow_ups=[Turn(message="and then?").model_dump()])
    findings = _plan_gate_findings(probe, PlanGateRun(probe_id=probe.id), GATED)
    assert len(findings) == 1
    assert findings[0].ok is False


# --------------------------------------------------------------------------- suite B · the ordering


def _degraded_outcome(*events: dict[str, object]) -> ProbeOutcome:
    """Run one turn against a scripted stream and hand back its outcome."""
    return _run_one(_probe(expects_tools=["compute_reaction_energy"]), *events)


def test_the_outage_announced_before_the_first_token_passes() -> None:
    """REV-6's actual claim: the model must learn the surface is short *before* it answers."""
    outcome = _degraded_outcome(
        {"type": "capability_degraded", "connectors": ["durable-jobs (Temporal)"]},
        {"type": "tool_call", "tool": "compute_reaction_energy", "arguments": "{}"},
        {"type": "token", "text": "The durable backend "},
        {"type": "answer", "text": "The durable backend is unreachable."},
    )
    assert outcome.first_degraded_index == 1
    assert outcome.first_output_index == 3
    findings = {f.check: f for f in degradation_findings(_probe(), outcome)}
    assert findings["announced before the first token"].ok is True


def test_an_outage_announced_after_the_answer_has_started_fails() -> None:
    """An outage announced after the answer has started fails.

    Every other field matches the passing stream; only the position differs, and a late announcement
    comes after the model has already planned against the surface. `failed_loudly` is false on both,
    because an announcement is not a failure, so ordering is the only thing distinguishing them.
    """
    outcome = _degraded_outcome(
        {"type": "tool_call", "tool": "compute_reaction_energy", "arguments": "{}"},
        {"type": "token", "text": "Running the calculation now"},
        {"type": "capability_degraded", "connectors": ["durable-jobs (Temporal)"]},
        {"type": "answer", "text": "Running the calculation now."},
    )
    assert outcome.degraded == ["durable-jobs (Temporal)"]
    assert outcome.failed_loudly is False
    findings = {f.check: f for f in degradation_findings(_probe(), outcome)}
    assert findings["the outage was announced"].ok is True
    assert findings["announced before the first token"].ok is False


def test_a_turn_with_no_degradation_reports_the_ordering_as_untaken() -> None:
    """A turn with no degradation event reports the ordering as untaken, a miss.

    The suite runs with the broker stopped, so no announcement means the front door stopped probing
    Temporal or the lane was misconfigured.
    """
    outcome = _degraded_outcome(
        {"type": "tool_call", "tool": "compute_reaction_energy", "arguments": "{}"},
        {"type": "answer", "text": "Done."},
    )
    findings = {f.check: f for f in degradation_findings(_probe(), outcome)}
    assert findings["the outage was announced"].ok is False
    assert findings["announced before the first token"].ok is False


def test_a_degraded_turn_that_never_answers_is_not_scored_as_correctly_ordered() -> None:
    """Degraded and then silent satisfies "before the first token" only vacuously.

    Reporting that as a pass would be the harness's own kind of fabrication: the claim is about
    what the model was told in time to use, and a turn that produced nothing used nothing.
    """
    outcome = _degraded_outcome(
        {"type": "capability_degraded", "connectors": ["durable-jobs (Temporal)"]},
        {"type": "tool_call", "tool": "compute_reaction_energy", "arguments": "{}"},
    )
    findings = {f.check: f for f in degradation_findings(_probe(), outcome)}
    assert findings["announced before the first token"].ok is False


def test_an_answer_with_no_token_stream_still_gives_the_ordering_something_to_read() -> None:
    """`answer` counts as output, so a non-streaming deployment is measurable too.

    Watching only for `token` would report the claim as unmeasurable on exactly the turns where it
    is easiest to satisfy — which is the shape of a check that always passes.
    """
    outcome = _degraded_outcome(
        {"type": "capability_degraded", "connectors": ["durable-jobs (Temporal)"]},
        {"type": "answer", "text": "The durable backend is unreachable."},
    )
    findings = {f.check: f for f in degradation_findings(_probe(), outcome)}
    assert findings["announced before the first token"].ok is True


def test_the_durable_launcher_must_actually_have_been_reached() -> None:
    """The durable launcher must actually have been reached.

    `capability_degraded` arrives on every turn when the broker is down, so a turn answering from
    memory would otherwise pass the ordering check.
    """
    outcome = _run_one(
        _probe(expects_tools=["compute_reaction_energy"]),
        {"type": "capability_degraded", "connectors": ["durable-jobs (Temporal)"]},
        {"type": "answer", "text": "Ammonia synthesis is exothermic, about -92 kJ/mol."},
    )
    findings = {
        f.check: f
        for f in degradation_findings(_probe(expects_tools=["compute_reaction_energy"]), outcome)
    }
    assert findings["the durable launcher was reached"].ok is False


def test_a_failed_check_is_visible_in_the_report_rather_than_a_missing_row() -> None:
    """A check that could not be taken is a FAIL row with its reason, never an absent one."""
    report = _findings_report(
        "title",
        "preamble",
        [
            Finding(probe_id="pg-01", check="refused", ok=True, observed="refused x"),
            Finding(probe_id="pg-01", check="executed", ok=False, observed="ran nothing"),
        ],
        ["a note"],
    )
    assert "**FAIL**" in report
    assert "ran nothing" in report
    assert "1/2 checks passed" in report


# ------------------------------------------------------------------ the corpus as declaration


def test_every_declared_suite_has_its_probe_file() -> None:
    """A suite whose file is missing would run zero probes and report zero failures."""
    for suite, filename in _M12_SUITES.items():
        assert (M12_DIR / filename).is_file(), f"suite {suite} has no {filename}"


def test_the_m12_directory_is_invisible_to_the_corpus_run() -> None:
    """The `m12` directory is invisible to the corpus run.

    `load_probes` globs one level; scripted conversations joining `make live-probes` would change
    what it measures without changing what it reports.
    """
    corpus = {probe.id for probe in load_probes(str(M12_DIR.parent))}
    m12 = {probe.id for probe in load_probes(str(M12_DIR))}
    assert m12
    assert corpus & m12 == set()


def test_the_m12_corpus_loads_with_unique_ids_across_its_files() -> None:
    """The same duplicate-id gate the corpus has: two probes sharing an id overstate coverage."""
    probes = load_probes(str(M12_DIR))
    assert len({probe.id for probe in probes}) == len(probes)


def test_every_m12_probe_file_carries_nothing_but_probes() -> None:
    """A stray top-level key would be silently ignored by a looser reader."""
    for path in sorted(M12_DIR.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(payload) == {"probes"}, f"{path.name} has unexpected top-level keys"
        ProbeSet.model_validate(payload)


def test_every_expected_tool_in_the_m12_corpus_exists_on_the_agent_surface() -> None:
    """A probe expecting a tool the agent cannot resolve can never pass.

    The same declaration-versus-surface check the corpus gets, for the same reason: without it a
    typo reports as a defect in the system.
    """
    surface = available_tool_names()
    unknown = {
        tool
        for probe in load_probes(str(M12_DIR))
        for tool in probe.expects_tools
        if tool not in surface
    }
    assert unknown == set(), f"m12 probes expect tools that do not exist: {sorted(unknown)}"


def test_the_plan_gate_probe_scripts_an_approval_and_a_plan_change() -> None:
    """The shipped probe must carry all three turns, or the suite it names cannot run.

    Asserted against the file rather than trusted: a probe that lost its third turn would still run
    clean and would silently stop testing DARK-1, which is the only reason the suite exists.
    """
    probes = _m12_probes(str(M12_DIR), "plan-gate")
    assert probes
    for probe in probes:
        assert [turn.before for turn in probe.follow_ups] == ["approve_plan", "none"], probe.id


def test_the_degradation_probe_carries_the_mock_selector_that_makes_it_runnable_offline() -> None:
    """The degradation probe carries the `[[d-collide]]` selector, so it runs with no LLM calls.

    `cli/mock_llm` picks a behaviour by the marker inside the message; carrying it in the question
    leaves one place to say which scenario a turn ran.
    """
    from chemclaw.cli.storm_behaviours import BEHAVIOURS

    names = {behaviour.name for behaviour in BEHAVIOURS}
    for probe in _m12_probes(str(M12_DIR), "degradation"):
        selectors = {name for name in names if f"[[{name}]]" in probe.question}
        assert selectors, f"{probe.id} carries no mock selector, so it cannot run without a model"


def test_the_m12_probe_directory_is_the_configured_one() -> None:
    """The suites read `settings.live_m12_probe_dir`; the tests read a path. Pin them together."""
    assert Path(settings.live_m12_probe_dir).resolve() == M12_DIR
