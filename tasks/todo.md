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

## Review round 2 — a code review of the whole diff, and what it caught

Three findings, all real, all mine; all fixed in the same branch.

1. **The date fallback was the very defect this change is about.** `DatedIngest` put the entry's
   *write* time into `performed_at` with nothing marking it, so `is_timeline()` went true and the
   campaign note asserted "Runs in the order they were performed" over what may be one afternoon of
   transcription. Both the docstring and the ADR said `ordering_caveat` "already exists to describe"
   the weakening; it did not — it only ever distinguished *missing* dates. Fixed at the source:
   `OrdReaction.date_source`, stamped by the seam, carried through `ProgressionStep`, and a caveat
   that says "in the order they were **recorded** … not proof of the order they were run".
2. **The binding guard was too broad.** It refused *any* transform on `hypothesis`, so an
   `OBJECTIVE` column with `{strip: {}}` — the case its own docstring called untouched — failed at
   worker startup, accused of carving intent out of prose. Narrowed to the three transforms that can
   put text in the field the cell does not hold: `regex`, `value_map`, `default`.
3. **An exhaustiveness claim that was only a comment.** `_STATED_OUTCOMES[...]` said a fourth
   `OutcomeClass` member "fails to type-check here"; mypy does not check a dict literal's keys, so it
   would have type-checked clean and raised `KeyError` inside `record_from_ord_reaction` — outside
   the reject-and-continue path, aborting the sync over one row. Now a `match` with `assert_never`,
   **verified by adding a fourth member and watching mypy name it**, then reverting.

The pattern across all three: *prose asserting a protection the code did not implement.* Two of them
were in text I wrote in the same change that exists to stop exactly that. Re-verified after: lint,
`mypy --strict`, 4768 passed / 3 skipped with Postgres up, seven validators green.
