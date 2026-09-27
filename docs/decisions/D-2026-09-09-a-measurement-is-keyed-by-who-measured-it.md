# D-2026-09-09-a-measurement-is-keyed-by-who-measured-it — A measurement is keyed by who measured it

**Status:** accepted · **Date:** 2026-09-09 · Supersedes the reconciliation rule stated in
`infra/sql/030_measurements.sql`, which stands as history.

## Context

`measurements` was keyed `(property, input_hash)` with `source` as a plain column, and
`calibration.py` upserted `ON CONFLICT (property, input_hash) DO UPDATE SET value = …, source = …`.
`030_measurements.sql` argues that shape in one sentence: *"A re-measurement replaces the row rather
than accumulating, because two values for one property of one molecule is a correction, not two
facts."*

That is true of a lab revising its own number. It is false of a replicate, a second solvent system,
a repeat after a failed run, or a second site — none of which is an edge case in chemistry.

Driven through the production `record_observation` / `calibration_for` against live Postgres,
against one prediction of −0.30 log S:

```
lab-basel    reports −0.10   scored=1  n=1  bias=−0.200
lab-shanghai reports −0.95   scored=1  n=1  bias=+0.650      <- sign flipped
measurements: [('solubility_logs','h-ethanol', −0.95, 'lab-shanghai')]   <- one row
```

The bias a chemist reads for *how far off is this calculator* moves from −0.200 to +0.650 on the
arrival of a second, equally valid number — at **n=1 both times**, so the count that `Calibration`'s
own docstring says exists to stop exactly this mistake ("a bias computed from three points is not a
bias") reports an honest 1 while concealing that a second observation was taken and destroyed. No
counter moves. The `source` column, the one field that distinguishes a correction from a replicate,
was written and then used only as the loser's epitaph.

**This tree has already taken this decision three times and each ADR stands**:
`D-2026-08-26-a-transcription-is-keyed-by-its-source` for `reaction_records`,
`D-2026-08-27-a-fingerprint-is-keyed-by-its-source` for `reaction_fingerprints`, and migration `051`
for `reaction_labels` before either. The first states the general form in one line: *"A column beside
the key does not represent that. It records which one won."* `measurements` was the fourth table with
that shape and the only one left.

Two further defects were found while fixing it, and both matter to whether the fix is a control at
all:

- `_RECONCILE_FROM_MEASUREMENT` (the measure-then-predict direction) was an unaggregated
  `UPDATE predictions p … FROM measurements m`. Once two rows can exist for one molecule that join
  takes whichever row the planner hands it first, so the scored value would not be stale — it would
  be **undetermined**.
- `report_measurement` hardcoded `source="chemist-reported"` and took no `source` parameter. At the
  only agent-facing surface that writes a measurement, every value carried one constant string, so
  keying on `source` would have been a control that never fires.

## Decision

**Row identity is `(property, input_hash, source)`, and reconciliation is the mean over sources.**

- A second value under the **same** source still replaces. A lab revising its own number writes one
  row, exactly as `030` wanted; the correction case is kept where it is true.
- One `_CONSENSUS` fragment (`avg`, `min`, `max`, `count(*)`, `count(DISTINCT unit)`, latest
  `observed_at`, the source list) serves all three readers, so the two reconciliation directions
  cannot disagree.
- `Calibration.n` keeps meaning *predictions scored*, not *measurements taken*. Changing it would
  make a version-over-version comparison stop being a count of comparable things. How many
  reporters there are is what `ObservedConsensus.sources` says, at the one surface a chemist reads.
- **The mean is a number nobody measured, so the spread rides beside it.** Two labs 0.85 log S apart
  average to −0.525, and a chemist told only the average cannot tell that from two labs that agree.
- `count(DISTINCT unit)` rides in the consensus and the reply refuses to quote a mean across more
  than one unit. Measured as 1 in every shipped path, because `report_measurement` reconciles into
  the ledger's unit before writing — asserted rather than assumed.
- `report_measurement` takes a `source`, defaulting to `chemist-reported`.

## What this costs, stated because it is real

**The default source still collapses.** Two unnamed chemists reporting the same property of the same
molecule still write one row. That is genuinely undecidable without the chemist naming a source, so
the reply says so rather than hiding it: it now names how many measurements are on file and under
which source, and states that reporting again under the same source replaces rather than adds. It
replaced `"Recorded; it reconciled 1 prediction(s)"` — a string that was byte-identical whether a
second fact had been stored or the first deleted.

`093_measurement_source.sql` drops and re-adds the primary key, so it is a
`_REVIEWED_ROLLBACK_BREAKS` entry: an image restored to before it cannot write the third key column,
and a row written under the new key whose `source` is not `chemist-reported` is unreachable to the
old reader's `(property, input_hash)` lookup. The operator recovery is to run the migration forward
again; nothing is destroyed by the rollback itself, because the widening added a column to the key
rather than removing information.

`CALCULATION_EPOCH` is **not** bumped: `measurements` is not `calculation_results`, no persisted
payload shape moved, and `tests/test_calc_payload_schemas.py` passes untouched. Bumping it would
discard every cached CREST search in the system for nothing.

## Alternatives declined

**Keep the collapse and document it.** It would have to argue the two-lab case rather than the
re-measurement case, and no such argument exists — the second lab's number is not a correction of
the first by any property the row carries. It would also require `record_observation` to report that
a prior value was discarded, which its `int | None` return cannot express.

**Make `n` count measurements.** Rejected above: it silently changes what a
calibration-over-time comparison compares.
