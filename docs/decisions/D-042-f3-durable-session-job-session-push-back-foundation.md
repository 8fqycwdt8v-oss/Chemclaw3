# D-042 — F3: durable session + job→session push-back (foundation-plan D-A3)

**Context.** Two gaps: a conversation died with the pod (in-memory history), and a finished job could
not reach a waiting chat (the user had to poll). F3 closes both without moving durability out of
Temporal (D-002) — session history and the push-back *notification* are their own layer.

- **F3-T1 durable history.** `agents/session_store.py::PostgresHistoryProvider` overrides only
  `get_messages`/`save_messages` (like `InMemoryHistoryProvider`), persisting `Message.to_dict()` to
  `session_messages` (`infra/sql/008`) keyed by session id, reloaded in `id` order. `build_agent`
  selects it via `_history_provider()` on `settings.session_store` (`memory` default | `postgres`);
  a fresh instance over the same DSN resumes the thread. `session_store_dsn` falls back to
  `postgres_dsn`.
- **F3-T2 push-back channel.** `session_events` (`infra/sql/009`, partial index over unconsumed) is a
  durable mailbox. `agents/session_events.py` is the writer (`record_session_event`), reader
  (`fetch_unconsumed`/`mark_consumed`), and a `stream_new_events` tailer whose fetch/mark/poll are
  dependency-injected so its consume-once loop is unit-tested with no DB. `workflows/notify.py` wraps
  the write in a Temporal activity (`record_session_event_activity`, on the background queue) plus a
  workflow-side `notify_session_best_effort` — same never-fail-the-science discipline as
  `publish_note_best_effort`.
- **F3-T3 wiring.** The turn's session is *ambient*, not a model argument: the runner stamps
  `agents/session_context.py`'s contextvar around the turn, and `submit_qm_job` reads it into
  `QMJobInput.session_id` (excluded from `qm_job_key`, so identical science still dedups across
  sessions and the completion notifies the launching session). The QM workflow calls
  `notify_session_best_effort` on completion; the front door exposes `GET /sessions/{id}/events` (SSE)
  streaming `job_completed` as a `JobCompletedEvent`, so a finished job wakes the chat with no polling.
- **Offline-tested with fakes** (contextvar, submit stamping, runner stamp/clear, tailer loop, events
  endpoint, activity forwarding); the Postgres round-trips and the Temporal workflow-emit prove out
  against live infra (they skip in the sandbox, joining the existing durable-layer skips).
- **Deferred (needs the live harness loop):** flipping the harness `awaiting` todo on completion
  (MAF TodoProvider store mutation) and emitting `PlanEvent`/live `JobStartedEvent`.
