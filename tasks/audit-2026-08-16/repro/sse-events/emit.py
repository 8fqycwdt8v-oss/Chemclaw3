"""Emit exactly what the backend puts on the wire, one JSON per line, tagged by case."""
import json, sys
sys.path.insert(0, "/home/user/Chemclaw3/src")
from chemclaw.api.events import (
    PlanEvent, TokenEvent, AnswerEvent, ToolCallEvent, ToolResultEvent,
    ErrorEvent, EvidenceSourceEvent, JobCompletedEvent, HandoffEvent,
    CapabilityDegradedEvent, QueuedEvent,
)
cases = {
  "plan": PlanEvent(todos=["[ ] run xtb"], plan_hash="9f2c1ab3deadbeef"),
  "token_main": TokenEvent(text="The answer is "),
  "token_subagent": TokenEvent(text="(subagent scratch prose)", agent="subagent"),
  "answer_full": AnswerEvent(text="ok", confidence=0.42, unsupported_claims=["x"],
                             review_required=True, verified_by="judge",
                             challenged=True, review_hold_id="hold-7"),
  "tool_call": ToolCallEvent(tool="predict_pka", arguments='{"smiles":"CCO"}', agent="subagent"),
  "tool_result": ToolResultEvent(tool="ich_impurity_limit", preview="PDE 100 ug/day",
                                 note_ids=["N-1"], numbers=[100.0], result_ref="ab12", agent=""),
  "error_loopcap": ErrorEvent(message="partial", code="loop_cap_reached", retryable=False,
                              correlation_id="c0ffee"),
  "evidence_source": EvidenceSourceEvent(source="kg", chunks=7),
  "job_completed": JobCompletedEvent(job_id="j1", summary={"job_id":"j1","converged":True}),
  "handoff": HandoffEvent(to="evidence", reason="needs lit"),
  "capability_degraded": CapabilityDegradedEvent(connectors=["eln","durable-jobs (Temporal)"]),
  "queued": QueuedEvent(),
}
for name, ev in cases.items():
    print(json.dumps({"case": name, "sse_event": ev.type, "data": ev.model_dump_json()}))
