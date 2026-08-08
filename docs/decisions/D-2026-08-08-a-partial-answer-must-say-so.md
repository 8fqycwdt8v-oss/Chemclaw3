# D-2026-08-08-a-partial-answer-must-say-so — seven science defects that render as clean results

**Status:** accepted · **Scope:** `science/`, `memory/`, `evals/`

## Context

The 2026-08-08 review campaign's science lane found seven defects, each reproduced by executing the
real code against the live Postgres. They are not one bug, but five of the seven share a shape the
repo already has a name for and a fix idiom for: **a degradation that produces the same bytes as a
clean result**. `FingerprintSearch.index_empty` and `ScreenResult.verdict` exist because of it, and
D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed settled the write half of it. The other
two are identity defects — a key and an id that cannot tell two different things apart — which is
the same failure one layer down: a stale value renders as a fresh one.

Each was measured before it was fixed, and one claim attached to this lane was **refuted** by
measurement (below).

## Decision

### 1. A truncated substructure scan is reported in the payload, not the log

`find_substructure_matches` scanned only the first `substructure_scan_max_records` rows by id and
returned `verdict: "…this is a genuine negative result"`. Measured: a 21-record store with the cap
at 20 and the sole azide at id `900` gave `hits: []`, `index_empty: false`, and that sentence.

`FingerprintSearch` now carries `scan_truncated` (not every record was examined) and
`hits_truncated` (the scan stopped at the result cap), and `verdict` reads `SEARCH INCOMPLETE` on a
truncated miss and `PARTIAL RESULT` on a truncated hit list. `_scan_for_matches` returns whether it
stopped early rather than letting the caller infer it from `len(matches) == cap`, because a corpus
holding exactly `cap` matches is complete and calling it partial is the same untruth reversed.

Carried on the result for the reason `store.py:100-127` already gives at length: a caveat outside
the payload has zero effect on the model that writes the answer. This is the tool whose entire job
is "have we seen this before".

### 2. The pKa cache key names the optimizer that relaxed the base

The base branch relaxes both species through `optimize_structure(OptSpec(...))`, and none of that
spec reached the key. Measured: `xtb_engine` tblite vs xtb changes `OptSpec.engine` and leaves the
pKa key **byte-identical**; and the un-keyed settings move the number — pyridine came out at
5.400052 / 5.402952 / 5.335181 for gradient tolerances 5e-4 / 5e-3 / 2e-2, all under one key.

`relaxation_spec()` is now the single construction of that spec, used by `_relaxed_energy` *and* by
the new `pka_cache_key`, so the key names the spec that actually runs. Its programs fold into
`calc_version()` and its knobs into `params`, the same split `XtbSpec.cache_key` makes.
Unconditional rather than only on the base branch: which branch runs is decided by the molecule
*after* the key is built, and a key that re-derives the dispatch is a key that can disagree with it.
The cost is that an acid result is invalidated by an optimizer change that could not have touched
it — recomputing more than necessary, never serving a stale value.

**A second, independent instance of the same defect was found while fixing it.**
`xtb_opt_trust_radius` was read from settings inside `_preconditioned_leg`, so it was in *no* key at
all, including `xtb.opt`'s own. Measured on ethanol: 0.35 and 0.05 relax to different geometries
(`st_e868cd6fe533107f` vs `st_860015aca7be952c`) and different energies under one key — and a
structure id is what every downstream key is built from. It is now an `OptSpec` field, which is
exactly the property `XtbSpec` was designed for: keyed by construction, not by review.

### 3. The peroxide widening reaches the pair rule too

`peroxide`'s SMARTS had already been widened to `[OX2,OX1-][OX2,OX1-]` for sodium peroxide;
`oxidizer-with-reductant`'s `left` arm kept `$([OX2][OX2])`. Measured: `H2O2 + NaBH4` →
`['oxidizer-with-reductant', 'peroxide']`, `Na2O2 + NaBH4` → `['peroxide']` only. A strong solid
oxidiser with a complex hydride is the case that rule is named for, and it was the one that did not
fire. Same pattern, so no new false-positive class: the widening is already proven in-tree by the
structural rule, and the regression test pins that a carboxylate and a nitro group — both carrying
`[OX1-]` without an O–O bond — still do not fire.

### 4. A calibration read that fails, fails

`reconciled_for` swallowed every exception into `[]`, and `Calibration`'s zero defaults then
serialized `bias/mae/rmse = 0.0`. Measured: a dead DSN and a disabled ledger produce **identical**
payloads, both reading as a calculator that has never missed.

Two changes. The read raises — its only callers are `calculator_trust` and `calculator_outliers`,
whose entire deliverable *is* the read, so there was no primary result the swallow was protecting.
That is the read half of D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed, whose write
half was fixed in `record_observation` on the identical argument. And `Calibration` adopts the
`computed_field` verdict idiom that `ScreenResult`, `FingerprintSearch`, `ImpurityLimitLookup`,
`Prediction`, `FitQuality` and `ScreeningDesign` all carry — it was the last advisory model without
one — distinguishing a disabled ledger from an empty one from too few points, with the three error
figures `None` rather than 0.0 when nothing was measured. The disabled state matters most:
`calibration_enabled` defaults to **False**, so the shipped deployment was the one reporting a
perfectly calibrated calculator.

`record_prediction` still swallows, and that is correct and unchanged: it rides along with a
calculation that must not be lost to a ledger fault, and no caller reports its outcome to anyone.

### 5. A campaign is identified by its space, not by the order it was written in

`campaign_id_for` hashed `parameters` and `categories` as ordered lists, so `[T, S]`, `[S, T]` and
`[T, S-reversed]` were three campaigns with three empty histories — while constraint *terms* were
already canonicalized against precisely this failure, with a comment saying so.

Both lists are now sorted in the identity payload. **Only the identity payload**, and that
distinction is measured: with a fixed seed, `[T, E, S]` and `[S, E, T]` propose byte-identical
candidates, so parameter order carries no information at all; category order *does* move the
acquisition optimizer slightly (`equiv` 2.1018 vs 2.0691 on one round, because a bare
`CategoricalInput` is ordinally encoded), so the problem the surrogate sees keeps the caller's
order. That jitter is far below experimental resolution; a split history is not. `objectives` is
deliberately **not** sorted — the lead objective is privileged, so their order is semantic.

**This moves three pinned ids once, deliberately.** All three baseline shapes in
`tests/test_bo_campaign_record.py` happen to be written unsorted, and each landed *on the id its
sorted spelling already carried* — captured from the pre-canonicalization code and now pinned in
both directions, so nothing new was minted and an already-sorted campaign keeps its row. Rows
written under the three pre-canonicalization ids are orphaned; that is a one-time cost against a
fork that would otherwise recur on every re-declaration. Recorded in `BACKLOG.md` with the operator
note.

### 6. A mining run is authoritative for the observations it emits

`_UPSERT` unioned `evidence_note_ids` and `projects_seen` with what was stored, so the arrays could
only grow. A reaction re-assayed SUCCESS is dropped by `mine_corpus` before fingerprinting, yet its
note id stayed in the evidence and kept counting toward `support`. Proved end to end against the
live database through the real mine → record → promotable chain: the observation stayed promotable
at support 3 while its own refreshed statement said "failed in 2 runs across 2 projects", so the
generated PR body contradicted itself in consecutive paragraphs and cited a documented success as
evidence of failure. `retire_stale` cannot reach it, because it is still being re-observed.

The upsert now replaces both arrays. This is safe precisely because it is not a general-purpose
store: both miners read the whole corpus every pass (`all_reactions()` reads from `datetime.min`),
so the cluster they emit *is* the current membership. Accumulation is unaffected and moves to where
it belongs — the miner sees more, so the row it reports holds more — and support now tracks the
corpus in both directions. After the fix the same probe drops the retracted member, falls to support
2, and leaves `promotable()`.

### 7. `expect_pass: false` is an assertion, not a mute

`regressions()` suppresses failures per case and can only ever detect a *failure*, so the one thing
it structurally cannot see is a gate that stops firing. Measured: raising `eval_efactor_max` and
`eval_pmi_max` to 1000 dropped `pharma-solvent-heavy` from the failure set — failed 4 → 2,
regressions 0, **exit 0** — and both pinned assertions in `tests/test_evals.py` still held, so the
whole green-chemistry gate went inert with no signal.

`EvalReport.inert_demonstrations()` names every case declared `expect_pass: false` whose gated
metrics all pass, `--strict` exits non-zero on it, and `render_report` says so in the report — a
gate that stopped firing is invisible by construction, because nothing appears in the failure table
to point at. *At least one* failing metric, not all: `retrieval-cross-coupling-literal-miss`
legitimately carries a passing `retrieval_precision` beside the `retrieval_recall` that is the
point. The per-metric `expect_pass` the finding suggested as the better form is not taken here —
it changes the case-file schema for no case that needs it yet (`BACKLOG.md`).

## Refuted by measurement

**The two "pre-existing `tests/test_pka.py` failures on `main`" do not reproduce**, and the pKa key
was not their cause. On unaltered `main` sources in a freshly resolved venv, `tests/test_pka.py` is
**27 passed** (320s); with this change, **29 passed** (291s). Structurally it could not have been
the cause either: `test_predicted_pkah_ranks_aromatic_bases_correctly` and
`test_in_sample_pkah_errors_are_far_below_the_acid_calibrations` both call `predict_pka` directly
and never touch a store, so no cache key is involved. The campaign's own alternate hypothesis — an
environment difference in the tblite numerics — is the one left standing.

## Consequences

- Three campaign ids move once; see §5 and `BACKLOG.md`.
- Every `pka` and `xtb.opt` cache entry is invalidated (the key widened, which is what a key
  widening is for). `engine_version`'s docstring already states this is correct.
- `calculator_trust` and `calculator_outliers` now surface a database outage as an error instead of
  an empty ledger. That is a behaviour change for the two tools and no others.
- `--strict` can now fail for a reason other than a regression. `make eval` (non-strict) is
  unchanged.

## Not fixed here

`peroxide-with-ketone`'s `[OX2H][OX2H]` misses `Na2O2 + acetone` for the same coordination reason
§3 fixes. Left alone deliberately: its explanation and citation are specifically about *hydrogen
peroxide* forming acetone peroxide, so widening the pattern without widening the prose would make
the explanation false for the molecules newly matched — and widening the prose is a chemistry claim
this change is not the place to make. Recorded in `BACKLOG.md` with the measurement and a trigger.
