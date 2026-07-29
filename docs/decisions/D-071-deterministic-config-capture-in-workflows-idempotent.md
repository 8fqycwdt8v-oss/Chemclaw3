# D-071 — Deterministic config capture in workflows; idempotent session events

**Context.** Two at-least-once/replay correctness gaps: `fan_out` read live
`settings.orchestrator_max_parallel_children` inside workflow code (replay after a config change
sees a different batch structure), and `record_session_event` had no idempotency key (an activity
retry after a committed-but-unacked insert duplicated the notification).

**Decision.** Workflow code must never read live settings where the value shapes command
structure: such values are captured once via a local activity (`resolve_fan_out_limit`) so replay
sees the recorded value. Settings reads that only shape command *attributes* (timeouts, queue
names) remain acceptable. Push-back events recorded from activities carry a deterministic
`dedupe_key` (`workflow_id:run_id:kind:payload-digest`) enforced by a partial unique index
(`infra/sql/014`), converting at-least-once retries into exactly-once notifications; NULL-key
writers keep plain append semantics.

**Consequence.** Replays are structurally deterministic under config drift, and duplicate
notifications from activity retries are impossible for workflow-recorded events.

**Result.** Proven against real Postgres (duplicate-insert dedupe) and via worker-registration +
history tests. `make lint type test` green.
