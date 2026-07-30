# Task: an intense review of the agentic system — performance, reliability, configurability, monitorability

Requested 2026-07-29/30. Branch: `claude/agentic-system-review-iu3gjz`.
ADRs: **D-145**, **D-151**, **D-152**, **D-153**.

The review itself is in `docs/archive/audit/2026-07-agentic-system-review.md`; its findings became
`REV-*` entries in `docs/planning/BACKLOG.md`. This file records the *closing* pass — the four items
left open because each needed a judgment call rather than an implementation, plus what verifying
them turned up.

(The previous occupant of this file, the codebase-cleanup pass, is merged and its record lives in
D-149, its backlog entries, and git history.)

## What shipped

- [x] **§1 Retention safety (D-145)** — `droppable_rows`, a union-find closure over `call_id` that
      contracts and never expands; `retention.py` routes `session_messages` through it; migration
      022 for the candidate scan.
- [x] **§2 Durable compaction (D-151)** — `plan_compaction`, a pure translation between MAF's
      annotate-and-insert and storage's delete-and-rewrite; run from `save_messages` in a second
      best-effort transaction, behind a default-off flag, with a `MAX(id)` watermark protecting the
      turn's own rows.
- [x] **§3 Push-back (D-153)** — `await_job_results` waits on Temporal workflow handles instead of
      destructively claiming the shared mailbox.
- [x] **§4 Metrics labels (D-152)** — declared labels, a per-counter series cap, `profile` on the
      five spend counters; per-model closed as already-solved by MAF's OTel emission.
- [x] **§5 REV-9 (D-152)** — no mechanism, a measurement: runbook §(viii), corrected HELP text,
      rewritten backlog entry.
- [x] **§6 Live harness smoke test (D-152)** — the first turn of the production construction path
      against a live model. It found a High.
- [x] Both corrections owed from earlier batches (D-143's `_SELECT` citation; the review report's
      §4).

## Review

**Verification changed three of the four open items, and every change was in the same direction:**
the backlog entry was written from a reading, and the code said something slightly different —
usually worse.

- REV-4's hazard was worse than documented *and already live*: retention could strand a
  `function_result`, which nothing can see and nothing repairs — a permanently bricked session.
- REV-7 had a second consumer that *destroys* events rather than losing them to a crash.
- REV-9 had been measured on the wrong provider, and its "cacheable half" is not cacheable through
  `Agent` at all.
- Half of REV-10 was already done, upstream, by a system nobody had looked at.

**Two dormant live defects, both fixed while dormant.** Retention (D-145) and the mid-turn wait
(D-153) are each behind a default-off flag. That is the cheapest possible moment to fix a
data-destroying bug, and it is the same argument REV-12 made.

**My own §3 design was refuted by exploration and withdrawn, not pushed through.** "Select, yield,
then confirm" re-selects an unconfirmed event on every poll, because the tailer has no
`try`/`finally`. Preventing that *is* a visibility timeout — which is what the backlog entry I was
trying to shortcut had already concluded. The withdrawal is in D-153 rather than edited out.

**Six refuted leads are kept in the record**, including one refuted by this review's own earlier
fix and one where the guard I proposed already existed.

**The smoke test earned its place.** `CHEMCLAW_HARNESS_ENABLED=true` ships in the chart; the code
default and all 2068 tests run `false`. The first live turn under the shipped configuration crashed
before reaching the model, and `make chat` had been unusable under it for as long as the flag has
been in the chart. Ten minutes of running the real entrypoint under the real configuration. The
general lesson — *a configuration only production sets is a configuration nothing tests* — is in
`tasks/lessons.md` with a rule that makes it mechanical, and the offline regression test now
reproduces that exact `RuntimeError` without a credential.

**One thing I would do differently.** I wrote the metrics label support with a rendering branch that
papered over an ambiguity (a bare sample beside labelled ones) instead of removing the ambiguity.
The elegance pass caught it and the fix was smaller than the workaround: make the declaration bind
in both directions, and the branch disappears. The prompt for that pass is in `CLAUDE.md`; it paid
for itself here.

## Verification

Every fix has a test **verified to fail on the unfixed code** by reverting the source — including
the two that carry the real burden: a straddling call/result pair surviving retention, and a
session's row count plateauing across 40 turns where it previously grew by 4/turn. When the
plateau assertion caught a local peak, I measured the real curve over 80 turns rather than widening
the threshold, found it oscillates in a band, and rewrote the assertion against the linear count.

`make lint`, `make type`, `make test` green against live Postgres: **2068 passed, 32 skipped** (19
Temporal test server, 13 xtb/crest binaries — none Postgres). Migration 022 applied.

## Still open, recorded rather than done

- **REV-7's original** — a notification lost between claim-commit and delivery. Needs a
  visibility-timeout lease, a migration, a per-*stream* holder id, a cancellation-shielded confirm
  (D-130's trap), and `event_id` in the SSE payload. It is an operator-facing contract change, so it
  gets its own ADR.
- **Healing stranded `function_result`s on read** — the only repair path for a session already
  bricked by the retention bug, deliberately not shipped alongside D-145 because it would *mask* a
  regression in the closure primitive rather than surface it.
- **A recurring live check of the harness path.** The smoke test closes "never executed", not
  "tested". CI has no credential, and inventing one is a decision about secrets.
- **REV-9's mechanism** — upstream in `agent_framework`, both halves.
