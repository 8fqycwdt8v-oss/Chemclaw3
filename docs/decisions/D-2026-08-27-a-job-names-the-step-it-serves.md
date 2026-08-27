# D-2026-08-27-a-job-names-the-step-it-serves — a launched job carries the plan step it was launched for

## Status

Accepted. Closes the `BACKLOG.md` §3 row "A surface cannot tell a waiting plan from a stalled one",
which had already been restated once from a fix that must not happen (see Context).

## Context

Under the harness, a plan is `TodoListMiddleware`'s todo list and a durable job is a
`job_records` row plus a `job_started`/`job_completed` push-back. Nothing joins the two. A chemist
watching an approved plan run sees "job started (id …)" in the feed and a checklist above it, and
has no way to tell which step that job serves — so a plan whose step 2 launched a six-hour CREST
search is indistinguishable from a plan that stalled after step 1.

The link used to exist and was removed on purpose, twice. Under MAF, a todo waiting on a durable
job was marked by prefixing its `content` with `awaiting-job:` — bookkeeping written into the plan
itself. D-2026-08-11 deleted the convention and D-2026-08-12 re-confirmed the deletion, because the
plan's identity is `plan_identity` over the bare `content` strings and a durable approval row is
keyed on that hash: any launcher that edits a todo revokes the very approval that authorized it.
`agent/state.py` records the consequence as a rule — `todos` holds the plan and only the plan.

So the BACKLOG row's first framing ("restore the marker") was rejected in the row itself, and what
it asks for after restatement is a **link from the job to the step** rather than from the step to
the job.

## Decision

The link is a **stamp on the job, taken at launch, from the turn's own view of the plan**. The
todo list is never written.

Three parts, one direction of data flow:

1. **A tool-call middleware publishes the plan's launch context.** `agent/plan_link.py`'s
   `stamp_plan_link` runs inside the governed tool chain whenever the harness is enabled for the
   profile. Before the tool body runs it reads `request.state["todos"]` — the same live, in-turn
   view the plan gate reads — and binds two ambient strings for the duration of the call
   (`core/plan_context.py`, the `session_context` pattern): the `content` of the first
   `in_progress` todo, and `plan_identity` over the bare contents. Both reset when the call
   returns, so nothing leaks across calls or turns.

2. **Launchers read the ambient link the way they already read the session id.**
   `connectors/jobs.py` copies it onto `ConnectorJobInput` (`plan_step`, `plan_hash` — additive,
   defaulted, replay-safe on the Temporal wire), and `job_record_for` threads it into the
   `JobRecord`, so `job_records` gains two columns (migration 057, additive with defaults). The
   summary projection carries `plan_step` too, so the jobs listing can answer "which step was this
   run for" without opening the full record.

3. **The stream says it live.** `record_job_started` folds the ambient step into `JobSignal`, and
   `JobStartedEvent` gains `plan_step` (additive, defaulted) — so every launcher that announces a
   job announces its step with it, including the report and memory-synthesis launchers that write
   no connector job record.

**The inference rule**: the linked step is the first todo whose status is `in_progress` at the
moment of the call. No `in_progress` step — or no plan, or a caller outside the graph (a template
step, the CLI) — stamps the empty string, which reads as the honest "this job was not launched
from a plan step". Two steps with identical `content` are indistinguishable, deliberately: they
are indistinguishable to the approval hash and to the chemist reading the checklist too.

`plan_hash` is stamped beside the step so a later reader can tell which *revision* of the plan the
step belonged to — a job from a superseded plan matches no current step, and the hash is what says
so rather than a fuzzy text match.

## Alternatives rejected

- **An explicit `plan_step` argument on every launch tool.** More accurate when steps run in
  parallel or the model launches ahead of its own bookkeeping — and it changes every launch tool's
  schema, spends context on every turn, and trusts the model to fill it truthfully on a field that
  joins to the audit trail. Reopen if in-flight parallel steps are observed in practice; the stamp
  and the argument can coexist (argument wins) without unwinding this design.
- **Reading the checkpointed plan at launch** (`plan_state.session_todos`). One superstep staler
  than `request.state` and blind to statuses; the middleware sees the same state the gate enforces
  against, which keeps the two from disagreeing about what the plan was.
- **A join table.** Two strings on a row that already carries `session_id` and `correlation_id`
  is the same shape those took; a table would be an abstraction with one caller.

## Consequences

- The plan gate is untouched: `plan_identity` still hashes bare contents, an approved plan still
  survives its own launches, and the stamp middleware only reads state. A test drives a launch and
  asserts the plan hash before and after are identical.
- Rows and events written before this decision decode with empty stamps and mean "the run did not
  say" — the same contract `payload_kind` set.
- A subagent's tool call sees no `todos` key (`SubAgentMiddleware` strips it) and stamps empty
  rather than reaching for the checkpoint; the specialist team is deleted, so this is a
  future-proofing note, not a live path.
- `Chemclaw3_ui` can badge the checklist item whose text matches a `job_started` event's
  `plan_step` — live only; restoring badges for still-running jobs after a reload needs a
  running-jobs listing, which `job_records` (finished runs only) cannot serve and which stays out
  of scope here.
