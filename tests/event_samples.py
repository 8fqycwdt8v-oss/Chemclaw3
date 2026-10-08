"""One instance of every turn event kind, in the two shapes a consumer meets.

`full` sets every field to a non-default value, so a renamed or retyped field changes its bytes;
`minimal` sets only the required ones, so a changed default changes its bytes. Shared by the wire
golden test and the schema test, so one list governs what "every event" means.
"""

from chemclaw.api.events import (
    AnswerEvent,
    ApprovalRequestEvent,
    AwaitingAnswerEvent,
    CapabilityDegradedEvent,
    ErrorEvent,
    Event,
    EvidenceSourceEvent,
    ExhibitDraftEvent,
    ExhibitEvent,
    HandoffEvent,
    JobCompletedEvent,
    JobFailedEvent,
    JobStartedEvent,
    NoteRecordedEvent,
    PlanEvent,
    QuestionEvent,
    QueuedEvent,
    ResultValue,
    TokenEvent,
    ToolCallEvent,
    ToolFailedEvent,
    ToolQueuedEvent,
    ToolResultEvent,
)

#: Keyed `<type>/<shape>`; the type is the SSE `event:` name and the `data:` discriminator.
SAMPLES: dict[str, Event] = {
    "queued/full": QueuedEvent(ticket=7, position=2),
    "queued/minimal": QueuedEvent(),
    "plan/full": PlanEvent(todos=["screen bases", "run DoE"], plan_hash="ab12", scope=["calc"]),
    "plan/minimal": PlanEvent(todos=[]),
    "tool_call/full": ToolCallEvent(
        tool="predict_pka", arguments='{"smiles": "CC(=O)O"}', agent="subagent"
    ),
    "tool_call/minimal": ToolCallEvent(tool="predict_pka"),
    "token/full": TokenEvent(text="The pKa is", agent="subagent"),
    "token/minimal": TokenEvent(text="hi"),
    "job_started/full": JobStartedEvent(job_id="qm-1", kind="calc", plan_step="optimise geometry"),
    "job_started/minimal": JobStartedEvent(job_id="qm-1"),
    "tool_queued/full": ToolQueuedEvent(tool="crest", job_id="j-9", state="queued", waiting=3),
    "tool_queued/minimal": ToolQueuedEvent(tool="crest", job_id="j-9", state="running"),
    "job_completed/full": JobCompletedEvent(job_id="qm-1", summary={"energy": -12.5, "ok": True}),
    "job_completed/minimal": JobCompletedEvent(job_id="qm-1"),
    "job_failed/full": JobFailedEvent(job_id="qm-1", reason="xtb exited 2: SCF not converged"),
    "job_failed/minimal": JobFailedEvent(job_id="qm-1"),
    "awaiting_answer/full": AwaitingAnswerEvent(
        request_id="r-1",
        state="waiting",
        subject="measured yields for plate 3",
        kind="measurement",
        asked_of="chemist@example.org",
        due_at="2026-10-09T08:00:00Z",
        reminders=2,
    ),
    "awaiting_answer/minimal": AwaitingAnswerEvent(request_id="r-1"),
    "capability_degraded/full": CapabilityDegradedEvent(
        connectors=["eln", "durable-jobs (Temporal)"]
    ),
    "capability_degraded/minimal": CapabilityDegradedEvent(connectors=[]),
    "note_recorded/full": NoteRecordedEvent(note_id="n-1", reference="notes/n-1.md"),
    "approval_request/full": ApprovalRequestEvent(prompt="Approve this plan?", approval_id="a-1"),
    "approval_request/minimal": ApprovalRequestEvent(prompt="Approve this plan?"),
    "question/full": QuestionEvent(question="Which Suzuki campaign?", options=["C-12", "C-14"]),
    "question/minimal": QuestionEvent(question="Which Suzuki campaign?"),
    "answer/full": AnswerEvent(
        text="pKa 4.76",
        checks_run=["verifier", "answer-shape"],
        confidence=0.62,
        unsupported_claims=["yield 91%"],
        review_required=True,
        verified_by="citation-gate",
        challenged=True,
        review_hold_id="h-1",
    ),
    "answer/minimal": AnswerEvent(text="done"),
    "tool_failed/full": ToolFailedEvent(
        tool="run_calc", message="refused", agent="subagent", reason="plan_gate", call_id="c-1"
    ),
    "tool_failed/minimal": ToolFailedEvent(tool="run_calc", message="boom"),
    "tool_result/full": ToolResultEvent(
        tool="predict_pka",
        preview="pKa 4.76",
        note_ids=["n-1"],
        numbers=[4.76, 1.6],
        values=[ResultValue(label="pka", value=4.76, unit="")],
        result_ref="tr-1",
        result_inline='{"pka": 4.76}',
        result_cut=True,
        agent="subagent",
    ),
    "tool_result/minimal": ToolResultEvent(tool="predict_pka"),
    "evidence_source/full": EvidenceSourceEvent(source="notes", chunks=4, failed=True),
    "evidence_source/minimal": EvidenceSourceEvent(source="notes", chunks=0),
    "handoff/full": HandoffEvent(
        from_agent="root", to_agent="analyst", reason="needs the DoE tool"
    ),
    "exhibit/full": ExhibitEvent(
        exhibit_id="e-1",
        revision=3,
        kind="table",
        title="Screen results",
        op="revised",
        author_kind="human",
        author="chemist@example.org",
        call_id="c-2",
    ),
    "exhibit/minimal": ExhibitEvent(
        exhibit_id="e-1",
        revision=1,
        kind="document",
        title="Report",
        op="created",
        author_kind="agent",
        author="agent",
    ),
    "exhibit_draft/full": ExhibitDraftEvent(
        call_id="c-3",
        op="revise",
        exhibit_id="e-1",
        kind="document",
        title="Report",
        markdown="# Report\n\nbody",
        done=True,
    ),
    "exhibit_draft/minimal": ExhibitDraftEvent(call_id="c-3", op="create", markdown="# R"),
    "error/full": ErrorEvent(
        message="the turn timed out", code="turn_timeout", retryable=True, correlation_id="abc123"
    ),
    "error/minimal": ErrorEvent(message="failed"),
}
