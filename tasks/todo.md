# Unblock the reaction-history user story — 2026-08-26

## The story
"Summarise the activities for reaction abc; where did development start and why was it
altered; list every change on a timeline with its rationale; recommend where to go next."

## The dataset it must run on
Materials and reaction SMILES arrive structured through an `ElnAdapter`. Everything else —
protocol, observations, the initial hypothesis — is **one free-text cell, completely free-form**.
A project code exists but is only reliable on well-kept projects; **reaction SMILES is the
grouping key**, and a reaction step is sometimes present. (Confirmed with the user, this session.)

## Plan
- [x] 1 · `performed_at` falls back to the entry timestamp, once, at the registry
- [x] 2 · `outcome_class` becomes optional — silence is not success (ADR)
- [x] 3 · The prose reader is asked for the run's intent, marked as read
- [x] 4 · A binding may not derive `hypothesis` from a transform chain
- [x] 5 · The campaign note drops columns nothing recorded
- [x] 6 · `eln-validate` validates the adapters that are actually attached
- [x] 7 · Verify: `make lint type test`, the validators, and a rendered end-to-end timeline

## Review

All seven done; `make lint type test` green (4806 passed, 3 skipped, Postgres up so nothing was
silently skipped) and every validator passes. `D-2026-08-26-silence-is-not-a-successful-run` is the
record.

**What the change actually was, in one line:** three defects of one shape — a value the source could
not supply was indistinguishable from a value it did.

**What building it found that the analysis had not.**

1. **A decorator cannot fix the outcome default.** The plan was one registry wrapper doing both the
   date and the outcome. It can only do the date: by the time `map_to_ord` returns, the SUCCESS
   default has already destroyed the distinction between "the adapter said success" and "nothing
   said anything". The outcome had to be a model change, and therefore an ADR.
2. **`observation_mining` was the dangerous consumer, not `playbook`.** `playbook` filters
   `is SUCCESS` and drops `None` for free. `observation_mining` filtered `is not SUCCESS`, which
   would have swept every unassessed run into a sentence counting how often a transformation
   *failed* — the old default with its sign flipped, and worse than the thing being fixed.
3. **The data-source discovery cache was cleared per-file, not in `conftest`.** One new test in
   `test_eln.py` pointing the registry at its own `tmp_path` poisoned the cache for the session:
   51 failures in four unrelated files. Two sibling caches were already in `conftest` for exactly
   this reason; the third has joined them and the per-file fixture is gone.
4. **The positional argument to `eln-validate` had to go.** With the validator reading the registry,
   a CLI arg that overrides one setting is a second way to set it — the duplication the config rule
   forbids. `CHEMCLAW_ELN_EXPORT_DIR` is the one way now.
5. **The campaign-table tests indexed cells by position.** With every column now conditional, a
   fixture that records no time shifts every assertion after it. They address by header name via a
   new `_cell` helper, so they test values rather than layout.

**Proved end to end** on the real schema (components + SMILES structured, everything else one
free-form cell): the mined note is a dated timeline naming all three condition swaps, and the
turn-time comparison carries a `Tested (read)` column quoting each run's stated aim — `—` for the
run that stated none, which is the honest answer.

**Left open, filed rather than implied** (`docs/planning/BACKLOG.md`): the turn-time comparison
cannot diff the components, because `reaction_records` keeps them only as prose in the body. The two
artifacts together answer the story, and `experiment-progression` already starts from the note, so
this is a seam to decide on rather than a break to patch.
