# Make the agentic backend lean to read

Scope: readability only. No behaviour change anywhere — every gate, event, refusal, metric and
audit row must be byte-identical in effect. The suite is the proof, not the argument.

## Why these three and not "cut the docstrings"

Measured before planning, because the obvious move is the wrong one. `agent/` holds **422
docstrings, 5,400 lines, median 9 lines, mean 12.8** — a healthy median with a long tail: **42
docstrings over 30 lines carry 1,770 of those lines**. So the prose is not uniformly bloated and a
mass cut would destroy the measurements this tree keeps deliberately. The reading cost is
concentrated in three structural places instead.

## 1 — `api/runner.py::run_turn` is one 483-line async generator

- [ ] `_TurnLedger` dataclass for the state that crosses stage boundaries (`answered`,
      `run_complete`, `answer_parts`, `started_jobs`, `tool_exchanges`, usage).
- [ ] `_turn_ambient(...)` sync `@contextmanager` stamping and resetting all five contextvars —
      makes "nothing in the teardown may await" structural rather than a comment.
- [ ] `_stream_into(...)` async generator shared by the main run and the mid-turn resume (the
      answer-part loop is currently written twice).
- [ ] `_resume_on_job_results(...)` for the mid-turn resume block.
- [ ] `_loop_cap_event(...)` / `_empty_answer_event(...)` returning `ErrorEvent | None`.
- [ ] `_book_turn_spend(...)` for the metrics + cost-ledger tail (sync, no await).
- [ ] `run_turn` left as orchestration a reader can hold in their head.

**Hazards, each of which the current code documents and must keep:** the teardown clause must
still catch `CancelledError` as well as `GeneratorExit` (D-130 — production disconnects arrive as
the former); the rollback predicate stays `run_complete`, never `answered`; `finally` must not
`await`; `consume_turn_approval` stays out of `finally`; `empty_answer` must still `return` rather
than fall through.

## 2 — Relocate the long-tail prose (42 docstrings > 30 lines)

- [ ] Each keeps purpose + why + a pointer; the historical narrative moves out.
- [ ] **Merged ADRs are never edited**, so narrative that spans modules goes to
      `src/chemclaw/agent/README.md`, and narrative that merely re-tells an existing ADR is
      replaced by a citation to it.
- [ ] `make prose-validate` must stay green (it resolves every named symbol, path and ADR id).

## 3 — Cut the import fan-out (165 first-party modules behind `api.runner`)

- [ ] Measure which edges pull the most, then defer imports at those seams only.
- [ ] Re-measure and record the number; no lazy import without a measured reduction.
- [ ] `mypy --strict` stays green and no import cycle is introduced.

## Verification

- [ ] `make lint type test` on a **full clone** with Docker up, so nothing skips for want of
      Postgres/Temporal. Report what skipped, if anything.
- [ ] PR, merge on green CI.

## Review

(filled in at the end)
