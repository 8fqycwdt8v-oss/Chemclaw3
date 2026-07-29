# D-077 — The turn stream emits its plan and its job launches (F2/F3 deferred item closed)

**Context.** `service/events.py` defines seven turn events; the web surface renders all seven; two —
`PlanEvent` and `JobStartedEvent` — were emitted by nothing since F2-T3 (ADR D-042 recorded the
deferral). The practical effect: a chemist who asked for a QM calculation saw silence between their
message and the answer, learning about the job only when its completion pushed back (F3-T3, possibly
a turn later); and the harness's plan — the whole point of an autonomous plan/execute backbone — was
invisible while it executed. Dead types also violate the repo's "no 'for later' stubs" rule: the
choice was emit or delete.

**Decision — emit.** Both inputs now exist offline, so emitting is the smaller diff than deleting a
contract two surfaces already render.

- **`JobStartedEvent`** — `agents/job_events.py`: a per-turn contextvar sink (`set_job_sink` /
  `announce_job_started` / `drain_started_jobs`), the same carrier and rationale as
  `agents/session_context` (task-local, so concurrent turns never cross; absent off the request path,
  where announcing to nobody is a no-op). `submit_qm_job` announces right where it already marks the
  awaiting todo; `run_turn` drains between streamed updates and once after the stream, so a launch in
  the closing update is not lost. A plain list, not a queue: the runner drains synchronously and
  nothing ever awaits it.
- **`PlanEvent`** — `agents.harness_todo.todo_titles` renders the todo store as `[x]`/`[ ]` lines
  (the read side beside the two existing mutators, so all todo-store access stays in one module);
  `run_turn` emits it only when the list *changed* since the last emission, so an unchanged plan does
  not flood the transcript.

**Only a genuine launch is announced.** The idempotent re-submit branch (`WorkflowAlreadyStartedError`)
returns an existing — possibly already completed — job id, which will never emit a matching
`job_completed` push-back; announcing it would leave a permanently "running" row in the UI. This is
the same reasoning that already governs the awaiting todo, kept consistent.

**A plan is a view, never a risk to the turn.** Off the harness path `_current_plan` returns `None`
rather than `[]` (an empty checklist reads as "the agent has no plan", not "this agent does not
plan"), and a malformed todo state is logged and skipped. No plan read can fail a turn.

**Not addressed (still open).** Resuming the *same* streamed turn mid-flight when a job completes
(the D-032/D-035 durable-approval seam) is untouched — this ADR makes the launch visible, not the
turn resumable.
