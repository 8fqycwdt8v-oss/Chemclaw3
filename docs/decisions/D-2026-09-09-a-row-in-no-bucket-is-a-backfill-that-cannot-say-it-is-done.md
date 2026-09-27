# D-2026-09-09-a-row-in-no-bucket-is-a-backfill-that-cannot-say-it-is-done — the publish walk accounts for every row it visits, previews what it will actually do, and refuses to scan with nowhere to publish

**Status:** accepted · **Date:** 2026-09-09

## Context

`D-2026-08-25-a-cache-is-not-a-record` built `python -m chemclaw.cli.backfill_publications` and the
`results` bundle's `republish_calculations` job over one shared walk (`publish/backfill.py`),
"because an operator and a chemist must cover exactly the same rows". Both report their coverage as
`(seen, queued, skipped)`, and the whole value of that report is that it says whether the corpus has
been covered — a backfill exists precisely because the corpus predates the sink, so nobody can go
and look.

Three defects, each reproduced against a migrated Postgres before it was touched.

**1. A row this release cannot *read* landed in no bucket, and the preview counted it as
queueable.** `calculation_results` is never pruned, so a deployment holds rows written by older
calculator versions — an `xtb.scan` row whose points carry `energy` where `_scan` subscripts
`energy_hartree` is the shape that actually exists, because `scan` was an `XtbTask` before
`D-2026-08-16-the-physics-leaves-the-cache-stays`. `outbox.enqueue_payload` catches every exception
from `records_for` (deliberately, and for a measured reason) and returns 0. `backfill.py` added that
0 to `queued` and touched nothing else. Measured on a four-row corpus holding one such row:

```
== dry run ==   cached: (seen=4, queued=3, skipped=1)
== real run ==  cached: (seen=4, queued=2, skipped=1)   # KeyError: 'energy_hartree' on k3
```

The operator-facing line read "4 row(s) seen, 2 queued, 1 skipped (no projector in this release)".
Subtraction could not recover the fourth either, because `queued` counted outbox *rows* while `seen`
counted table rows and one payload can decompose into several. Measured on the jobs walk over one
solvent screen, one refusable reaction payload and one unroutable row:

```
== jobs ==  dry (seen=3, queued=2, skipped=1)   real (seen=3, queued=3, skipped=1)
```

— a real pass whose queued count *equals* the rows seen while one of them was skipped, which is not
an arithmetic any reader can act on. And the dry run disagreed with the pass it previewed, because
it counted a row as queueable the moment `projector_for` returned a *route*, which is not an
attempt.

The same zero hid a second, healthier case: the enqueue is `ON CONFLICT DO NOTHING`, so a **second**
pass over a fully-covered corpus reported `(seen=3, queued=0, skipped=0)` — three rows in no bucket,
with nothing wrong at all.

**2. A pre-2026-08-26 refined ensemble published silently missing its two headline numbers.**
`c7035b66` renamed `RefinedEnsemble.conformational_entropy_cal_per_mol_k` to
`refined_conformational_entropy_cal_per_mol_k` (and `ensemble_correction_kcal` likewise).
`_refined_ensemble` reads only the new names through `.get()`, `_kept` drops a `None` fact, so a row
stored between migration 055 and that commit projected cleanly, counted as queued, and reached the
results store without either number:

```
published j1 (old shape): [total_conformers, refined_conformers, refined_population_covered, conformer_treatment]
published j2 (new shape): [... , refined_conformational_entropy, refined_ensemble_correction, ...]
```

No warning, no counter, no refusal — and a consumer cannot tell it from an ensemble that genuinely
had none.

**3. `republish_calculations` reported success with publishing switched off.** `enqueue` is a no-op
when `CHEMCLAW_RESULT_SINKS` is empty. The CLI has guarded that since it shipped and exits 1; the
durable job did not, so a full scan of two never-pruned tables returned

```
publishing_enabled: False
republish_calculations reports: {'calculations_seen': 10, 'calculations_queued': 0, ...}
rows actually queued: 0
```

which is exactly what a corpus with nothing left to publish reports — at the end of a job that can
run for hours, to the caller least able to diagnose it.

## Decision

**1. Every row a walk visits lands in exactly one bucket, and the record count is kept in its own
unit.** `backfill.WalkCounts` is `(seen, queued, skipped, failed, records)` with
`seen == queued + skipped + failed` as an asserted invariant. `skipped` is "this release has no
projector for it"; `failed` is "this release has one and it could not read the row". They are
separated because they ask for different actions: a skip needs no fix and never will, while a
failure is a defect in *this* release that recurs identically on every pass until code changes.
`records` is what the walk projected, not what the outbox wrote — the enqueue is an upsert and
writes one row *per sink*, so a rows-written count would make a re-run and a two-sink deployment
each disagree with the preview for reasons nobody asked about. `chemclaw_results_queued_total`
already meters rows written.

`queued` therefore means "the row's records reached the outbox", not "rows were written", which is
what makes the partition hold on the second pass as well as the first.

**2. A dry run projects.** It runs the same `outbox.project_payload` the real pass runs and stops
one step short of `enqueue`, so all four row counts agree exactly in both directions.

**3. `outbox.project_payload` is the three-state `enqueue_payload`'s `int` cannot carry.** It
returns the records, `[]`, or **`None`** when the projector raised — and `enqueue_payload` becomes
that plus the write, keeping its `-> int` signature and its never-raises contract for the three
hooks behind a finished calculation, none of which reports a number to anyone.

**4. The two renamed refined-ensemble fields are read through `_renamed`, with a WARNING.** The
fallback is allowed because the rename **was a rename**, and that was verified rather than taken
from the commit message: `c7035b66` changed two keyword names in `connectors/calc/compose.py` and
nothing else — `entropy = ensemble_entropy(populations, degeneracies)`,
`round(entropy, 3)` and `round(-temperature * entropy / 1000.0, 3)` are byte-identical before and
after, at the same line offsets, and `git diff c7035b66 HEAD` over that file touches neither.
`_refined_ensemble` is reachable only through `payload_kind="RefinedEnsemble"` — an exact model-name
lookup, with no `_CALC_TYPE_PROJECTORS` prefix routing to it — so the legacy name on a payload that
arrives here is the refined subset's own entropy; `ConformerEnsemble`'s field of that name means the
whole-ensemble quantity and is `_ensemble`'s row, never this one.

**5. `republish_calculations` refuses before it scans**, raising `ResultSinkError` — already in
`durable/publish._BAD_DATA_TYPES`, so the job fails fast with the reason in the push-back instead of
spending eight attempts on a setting no retry changes.

## What was measured, not argued

**The dry run can afford to project.** Reading a page and skipping the projection is **54.4 µs/row**;
projecting a payload costs a further **~274 µs** (pKa and a 40-member ensemble measured within 6 µs
of each other, so it is RDKit canonicalization rather than the shape); the pass being previewed is
**19,241 µs/row**, dominated by the round trip. So a projecting dry run is ~6x the old preview and
**1.7%** of the run it describes — on 1M rows, ~5.5 min against ~5.3 h. A preview that cost a
material fraction of the pass would have had to choose differently; this one does not.

**The rename was the only one of its class in this module, and no automated check could have found
it.** Two sweeps over `project.py`: every `payload.get("x")` against every field declared anywhere
in `src/` returns two names, both in `_dft`, the deliberately-kept projector for a deleted model.
Per-projector, every `payload.get` against the `model_fields` of the model it is keyed by returns
only the documented shared-projector unions (`_ensemble` over two ensemble shapes, `_optimization`
over two optimization shapes). The reverse sweep — model fields no projector reads — returns
`sampled` and nothing else. **`_refined_ensemble` is clean in both directions**, because the defect
is a name that is absent from the payload rather than one that is present in the code: it is
visible only in `git log`, which is why the fallback names its commit.

**The fixture that the old preview could not fail.** `tests/test_publish_backfill.py::_insert_cached`
wrote `{"pka": 4.2}` — no subject at all, which `_pka` cannot build a record from. Harmless while a
dry run only *routed*; a projection failure the moment one projects. It now writes a payload that
actually projects, because a fixture that cannot be read is the wrong control for "was this row
queued".

## Consequences

- `backfill_cached`/`backfill_jobs` return `WalkCounts` rather than a 3-tuple. Both callers are
  updated; `ConnectorJobResult.data` gains `calculations_failed`, `jobs_failed`,
  `records_from_calculations` and `records_from_jobs`.
- The CLI reports the partition on one line and the record count on another, in their own units,
  and raises the failure bucket to WARNING when it is non-zero — it is the one bucket an operator
  has to act on, and `outbox.project_payload`'s `logger.exception` has already named each row.
  The workflow summary names it only when it is non-zero: a line reading "0 unreadable" on every
  healthy pass is how it stops being read.
- **Nothing was lost by any of this and nothing is recovered by it either.** Neither source table is
  ever pruned, so a release that can read those rows re-runs the walk and picks them up. What
  changes is that "the backfill is done" stops resting on a number that could not see what it left
  behind.
- A legacy refined ensemble now publishes the same two numbers a current one does, under the current
  names. It is **not** flagged on the record: the value is the identical arithmetic and `calc_version`
  already distinguishes the producers, so there is nothing for a *consumer* to be told — the WARNING
  is for the operator running a backfill over a legacy corpus, who is a different reader.
- `tests/test_durable_heartbeat.py`'s republish test stubbed the activity with a hand-written key
  set, which is a second definition of the report that goes stale in silence — it now answers with
  what `_walk` itself produces.

## What was rejected

- **Refusing a legacy-named refined ensemble** so it lands in `failed` and is visible. It would make
  the row visible at the cost of discarding a recoverable, verified-identical number. The polarity
  flips the moment a rename moves the *quantity*: `_renamed`'s docstring states that as the rule
  binding whoever adds the second entry, and the test asserts the *values*, which is what says which
  kind of rename this is.
- **A `calculation_flag` row recording that a legacy field name was read.** `flag` is a free
  `VARCHAR(64)` with no registry FK, so it would cost no DDL — but nothing would consume it, and the
  published number is not different, only its provenance is. `calc_version` carries that already.
- **Widening `enqueue_payload`'s return to a tuple or a result object.** Its three other callers
  (`science/calc/store.py`, `publish/hooks.py`, `durable/publish_results.py`) genuinely do not care
  why a publish queued nothing: the science is persisted either way, which is the whole reason the
  function promises not to raise. Splitting the projection out gives the one caller that *is* a
  report what it needs, with no change to the three that are not.
- **Reporting rows written rather than records projected.** It is the number an operator intuitively
  wants and it is not comparable across a dry run, a second pass, or a second sink. The counter that
  answers "how much reached the queue" exists and is scraped.
- **Letting `republish_calculations` report "publishing is off" as a field in its counts.** A report
  nobody reads until the job ends is the failure mode being fixed, not a lesser form of it.

## References

- `docs/decisions/D-2026-08-25-a-cache-is-not-a-record.md` — the seam this walks, and the backfill it
  built.
- `docs/decisions/D-2026-08-16-the-physics-leaves-the-cache-stays.md` — why `xtb.scan` is a retired
  calculator whose rows a deployment still holds, which is what makes the failure bucket a real
  case rather than a hypothetical one.
- `docs/decisions/D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose.md` — the same argument
  one level out: a coverage number that is asserted rather than restated.
- `c7035b66` — the rename, and the diff that says it was one.
