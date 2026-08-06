# D-2026-08-06-what-a-job-is-and-what-a-record-is — What a job is, and what a record is

**Status:** accepted · **Date:** 2026-08-06

## Context

Three rows from the durability lane, which turn out to be two questions:

- **DARK-4 [M]** — the durable job idempotency key omits every versioned input, so a method change
  rejoins the completed prior run and returns numbers the old method produced.
- **Retention's eight unlisted tables [M]** — `session_owners`, `session_turns`, `turn_costs`,
  `predictions`, `measurements`, `note_proposals`, `plan_approvals`, `bo_suggestions`. Each wants a
  disposal decision, *not* a sweep that picks them up by default: "unlisted" must stop reading as
  neither decided nor deferred.
- **A pruned session keeps its listable identity [L]** — the owner row outlives the last message, so
  "what was I working on" returns an empty conversation.

## Decision

### A job's identity includes the settings that change its answers

`job_workflow_id` hashed `[connector, job, payload]`. Change `xtb_method` and the calculation store
correctly misses and recomputes, while `start_workflow` raised `WorkflowAlreadyStartedError`,
rejoined the **completed** prior workflow, and handed back the old method's numbers.
`science/calc/store.py` has always taken the opposite and correct position for the same
computations, with `calc_version` in its key.

**A manifest declares setting-name *prefixes*; the launcher reads their current values.** So the
values are derived and only the prefixes are declared, which is the split the seam forces: core
cannot ask a bundle which settings change its answers without importing the bundle's calculators
into the chat process, and that is the one thing D-118 forbids. `calc` declares `xtb_`, `crest_`,
`pka_`, `solubility_`.

Prefixes rather than a list of settings, because a knob added tomorrow is covered the day it is
added rather than the day someone remembers the declaration. A prefix also catches resource knobs
(timeouts, thread counts) that change no number, and **that error is the cheap one**: it costs one
workflow re-execution whose every activity hits the calculation cache and returns the same values
immediately (D-011). The opposite error returns wrong numbers silently.

That asymmetry is the whole argument. The two layers do different jobs — the workflow id dedups
*launches*, the calculation store dedups *computation* — and because the second exists, making the
first stricter is nearly free.

A bundle declaring nothing gets a byte-identical id to before, so no in-flight workflow is orphaned
and no fixture bundle in the suite is touched. `make connector-validate` refuses a prefix matching
no setting, because such a prefix reads as a declaration and contributes nothing to the id — which
is this exact defect returning with the fix apparently in place.

**Rejected: `deployment_revision` in the key.** Fully derived and needs no declaration, and it
re-runs every job on every deploy — including a rolling one, where two pods of different revisions
would compute *different* ids for the same job and both start it. For an `expensive: true` HPC job
that is a real bill for a config change that touched nothing.

### Each of the eight tables gets a decision, and four of them are "no"

Pruned, each with its own window (default 0, so a deployment states its policy — the rule the first
two tables already followed):

- **`session_turns`** is a *lease*. Its `expires_at` has passed by seconds in normal operation, and
  only the next claim on that session id ever overwrites the row, so a session nobody returns to
  keeps its dead lease forever. Dated by `expires_at`, not `claimed_at` — that is the column that
  says "this is over".
- **`turn_costs`** is an operational spend ledger, read by the admission check over a window of
  days. A row older than the longest such window is already unreadable by design.
- **`note_proposals`**, decided rows only. A pending proposal is the PR-gate's live queue, and
  `decided_at` is NULL for it — which makes the age comparison NULL and the row unselectable before
  the state check even applies, so the two guards are belt and braces.

Refused, by name, because each is a **record** and a record is what a retention policy keeps:

- **`predictions` / `measurements`** — the calibration ledger, the evidence every constant in
  `core/config/calculators.py` was fitted from. Ageing them out leaves the constants in place with
  nothing behind them, so the deployment can no longer answer "why is the slope 0.28733".
- **`plan_approvals`** — that a human signed off, which is the GxP line this system is arranged
  around (D-005).
- **`bo_suggestions`** — a campaign's evaluation record. D-157's argument applies unchanged: the
  table exists *because* that record used to expire with Temporal's history, and an age cutoff would
  restore exactly that failure one window later.

**`session_owners` is the ninth shape**: pruned by *emptiness*, not by age on its own rows. It is
therefore not in `_PRUNABLE`, whose map is `(date column, predicate)` and whose value is being
explicit and closed — expressing "has no rows in another table" there would have needed a wildcard
predicate column that the map exists to refuse.

### An empty session is not something to return to

The owner row outlives the conversation it names, so the sidebar offered sessions that open empty.
Two changes, each for its own reason and neither sufficient alone:

- The **listing** filters on remaining history (`EXISTS`, which stops at the first row — the
  question is "any history at all", not "how much"). Immediate: it must be right the moment the last
  message goes, not the next time the sweep runs.
- The **retention pass** collects the orphaned rows, so they do not accumulate behind that filter.
  It runs after the `session_messages` prune in the same pass, so a row orphaned by this pass is
  collected by this pass.

The collector carries an age bound sharing `retention_session_messages_days`. That is a **race
guard, not a policy**: an owner row is written before the session's first message, so emptiness
alone would delete a live session's ownership in the gap between those two writes.

## Consequences

- A method or calibration change makes the next launch a new run. An unchanged deployment still
  rejoins its own runs, which is what the key is for.
- One more manifest field, validated, and a bundle that ignores it behaves exactly as before.
- Every one of the eight tables is now either pruned or refused **by name**, in the module docstring
  and in `_PRUNABLE`, so a reader can tell a decision from an omission.
- `GET /sessions` stops offering conversations that open empty, and `session_owners` stops growing.
- Three new retention windows, all defaulting to off.

## Alternatives rejected

- **`deployment_revision` in the job id.** Re-runs everything on every deploy, and duplicates work
  across a rolling one.
- **A hand-written list of settings per bundle.** Drifts the first time a knob is added; the prefix
  cannot.
- **Deriving the prefixes from the bundle's calculators.** Would import the bundle's dependency
  closure into the chat process (D-118).
- **Sweeping the eight tables in by default.** The row's explicit ask was the opposite, and it is
  right: three of them turned out to be records whose disposal would destroy evidence.
- **Pruning `session_owners` by age alone.** Deletes ownership of a live session that has simply
  been quiet, and races with the row's own creation.
- **Filtering the listing only, without collecting the rows.** The table keeps growing behind a
  filter, which is how an unbounded table becomes invisible.

## Not in this change

The durability lane's other rows stay open, and each for a stated reason rather than by omission: a
failed run leaves no record, `request_development_report` writes no record, the 600 s heartbeat
coupling, Temporal namespace retention, and REV-7's visibility-timeout redelivery. The last two need
a live Temporal broker to verify — the test server is unreachable from this environment — and REV-7
is an operator-facing contract change the backlog already says wants its own ADR.
