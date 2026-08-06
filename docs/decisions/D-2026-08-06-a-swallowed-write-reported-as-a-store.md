# D-2026-08-06-a-swallowed-write-reported-as-a-store — A swallowed write, reported as a store

**Status:** accepted · **Date:** 2026-08-06

## Context

From the whole-codebase security sweep, error-handling lane. Three tools told a chemist their data
was kept when it was not. This is the family
`D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` named; these are further instances,
and one of them fires in the **default** configuration.

Everything below was measured. Prose is evidence about what its author believed.

## Decision

### 1. `report_measurement` said "Recorded" in every unconfigured deployment

`record_observation` returned `0` for two different things: "stored it, and nothing had predicted
this" (normal, informative) and "did nothing at all". The tool read the single `0` as the first and
answered:

> Recorded for CCO. Nothing had predicted pka for it yet, so no prediction was scored — **the
> measurement is kept** and the next prediction of it will be scored against this value.

`calibration_enabled` is `False` by default. So the tool said that on **every call in every
unconfigured deployment**, while no table was touched. Run directly, before the fix, that is
verbatim what it returned.

`None` now means "not stored"; `0` keeps its existing meaning of "stored, nothing matched". The
tool says plainly that the value was not kept and that an operator must enable the ledger.

**The swallow itself is the other half.** `record_observation` had inherited the `except Exception`
from `record_prediction` directly above it — and that one is *right*: a prediction row is advice
about work that already happened, so losing it must never cost the calculation. A measurement is
the entire deliverable of the call; there is no primary result to protect, and swallowing turns the
tool's only job into a false success. The same construct is correct in one function and wrong in the
one below it, which is why "audit every broad `except`" is not a mechanical exercise.

### 2. The preference tools confirmed durability they did not have

`remember`/`forget` write the in-memory copy first and always succeed, then try Postgres and
swallow. So the *current* session behaves correctly and the failure is invisible — while
`remember_preference` answered "Remembered … for this chemist" under a docstring promising "future
turns and future sessions".

Both now report what actually happened. Swallowing stays: a lost preference must degrade
personalization, not fail a turn. Only the claim changes.

`forget` is the worse direction and says so: the in-memory copy is dropped, so the preference looks
removed for the rest of the session and reappears from Postgres in the next one. A chemist who asked
for something to be forgotten and was told it was must not find it back.

### 3. An unreadable preference store is not an empty one

`recall` fell back to in-memory on a read failure. That is right when memory *has* something — it is
this process's own view of the same rows. It is wrong when memory is empty, because
`recall_preferences` documents an empty list as "nothing has been recorded yet", so the model tells
the chemist they have no preferences and the chemist restates them — into a store that is still
down.

An empty fallback after a failed read now raises. A failed answer is better than a wrong one; a
populated fallback is still used, and a test pins that line in both directions.

## Consequences

- `record_observation` returns `int | None` and no longer swallows database errors. A write failure
  reaches the connector's error sanitizer and is reported as a failure.
- `PreferenceStore.remember` and `.forget` return `bool` (durable as configured). In memory mode
  both return `True`, because memory *is* the configured store there — the flag means "as durable
  as this deployment asked for", not "in Postgres".
- `recall` raises when a failed read has no memory to fall back on.
- Nothing exercised `report_measurement`'s message before, which is why the default-configuration
  case survived review. It has tests now, and the `None`/`0` distinction is mutation-proven.

## Alternatives rejected

- **Returning `-1` (or another sentinel int) for "not stored".** An `int` that is sometimes a count
  and sometimes a status is the same collapse that caused this, one type later.
- **Raising when the ledger is disabled.** Disabled is a deliberate operator choice, not an error.
  It needs a different *answer*, not a failure.
- **Making the preference tools raise on a failed write.** A lost preference must not fail a
  chemist's turn — the existing comment is right about that. Only the confirmation was wrong.
- **Enabling `calibration_enabled` by default** so the message becomes true. That changes a
  deployment's storage behaviour to fix a wording defect, and would still say "Recorded" when the
  write failed.
