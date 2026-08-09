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
`hits_truncated` (more matched than the result list could hold), and `verdict` reads
`SEARCH INCOMPLETE` on a truncated miss and `PARTIAL RESULT` on a truncated hit list.

**Both flags were first shipped as exactly the `len == cap` inference this section condemned, and
that is corrected here.** `_scan_for_matches` returned `True` the instant the cap-th match was
appended, without ever asking whether another match existed, so `hits_truncated` equalled
`len(hits) == cap` for every input: measured at cap 3, a corpus of three matching records reported
`PARTIAL RESULT: … Do not report it as the complete set` over a scan that had seen everything. And
`scan_truncated` was literally `len(records) == cap`, so a corpus sitting exactly on the cap (5000
by default) turned a true "no azide on file" into `SEARCH INCOMPLETE … Report the search as
inconclusive` — the safety-shaped answer inverted, which a chemist learns to distrust just as fast.

Neither is inferred now; both are *observed*. The scan reads one row past the record cap and drops
it, so truncation means "there was another row", and it runs on past the result cap until it finds
a match it cannot return, so truncation means "there was another match". Measured at cap 3: 3
matching records → `hits_truncated False`, 4 → `True`, with `len(hits) == cap` in both. The extra
work is bounded by the record cap and the match timeout that already bound the miss path.

Two further cases the first version left silent are covered on the same argument. Similarity search
truncates at `fingerprint_top_k` (default 10) and set neither flag — 18 qualifying molecules
rendered as "10 indexed molecule(s) matched this query", a floor read as a total — so `find_matches`
now probes one row past the page and both similarity entry points carry the flag. And a stored
structure that no longer parses is skipped by the substructure scan, which is a record *not
examined*: it now counts toward `scan_truncated`, because the alternative is a corpus whose one
azide row has a malformed label answering "this is a genuine negative result".

Carried on the result for the reason `store.py` already gives at length: a caveat outside the
payload has zero effect on the model that writes the answer. This is the tool whose entire job is
"have we seen this before".

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

**Widening this key costs more than recomputation, and both costs are stated here rather than
discovered later.** `predictions` is keyed `(calc_type, calc_version, input_hash)` and
`reconciled_for` reads with an exact `calc_version` predicate (D-139), so every reconciled pKa
residual on file becomes unreachable the moment the version string moves: `calculator_trust("pka")`
reports `UNCALIBRATED`, n=0, until each molecule is predicted again. Nothing has to be re-measured
— `record_prediction` re-reconciles from `measurements` on write — but the ledger refills only per
molecule re-predicted, so it refills at the rate the calculator is used. A cache miss costs CPU;
this hides accumulated bench work, which is a different kind of cost and worth pricing before any
future widening. And under the default `xtb_engine=auto` the key now names `resolve_backend()`, so a
pod with the `xtb` binary and one without compute different pKa keys where the string previously
named only the tblite/RDKit wheels and was machine-independent. **That fleet partition is wanted**:
the two backends do not agree to the last decimal and the base branch really relaxes through
whichever is present, so a shared key would serve one program's number as the other's — the defect
this widening removes. A deployment that would rather not pay it pins `CHEMCLAW_XTB_ENGINE`.

The measured science consequence of the defect itself is small and deserves saying: the pyridine
spread is 0.068 pKa units, well inside the calculator's stated ±1.6. A real key defect — one key
serving three numbers — with a modest effect on any single answer.

**A second, independent instance of the same defect was found while fixing it.**
`xtb_opt_trust_radius` was read from settings inside `_preconditioned_leg`, so it was in *no* key at
all, including `xtb.opt`'s own. Measured on ethanol: 0.35 and 0.05 relax to different geometries
(`st_e868cd6fe533107f` vs `st_860015aca7be952c`) and different energies under one key — and a
structure id is what every downstream key is built from. It is now an `OptSpec` field, which is
exactly the property `XtbSpec` was designed for: keyed by construction, not by review.

That was pinned by a test asserting only that the *key* moves, which is half the claim and the less
important half: reverting the optimizer's `spec.trust_radius` to the settings read leaves that test
green while every explicit `OptSpec(trust_radius=…)` keys distinctly and runs the settings value —
"a key that disagrees with what ran", from the other side. `tests/test_xtb_opt.py` now relaxes
ethanol at both radii and pins that they reach different stationary points, and the mutation was
executed to confirm it fails there and passes the key test.

### 3. The peroxide widening reaches the pair rule too

`peroxide`'s SMARTS had already been widened to `[OX2,OX1-][OX2,OX1-]` for sodium peroxide;
`oxidizer-with-reductant`'s `left` arm kept `$([OX2][OX2])`. Measured: `H2O2 + NaBH4` →
`['oxidizer-with-reductant', 'peroxide']`, `Na2O2 + NaBH4` → `['peroxide']` only. A strong solid
oxidiser with a complex hydride is the case that rule is named for, and it was the one that did not
fire. Same pattern, so no new false-positive class: the widening is already proven in-tree by the
structural rule, and the regression test pins that a carboxylate and a nitro group — both carrying
`[OX1-]` without an O–O bond — still do not fire.

**The same standard, applied to the neighbours it was not applied to.** Three sibling rules carry an
identical salt/neutral asymmetry. One is fixed here and two are deliberately not, on the rule this
section states: widen where the rule's own explanation and citation already cover the newly matched
molecules, and file it otherwise.

Fixed: hydrazine is bought and weighed as its hydrochloride or sulfate, whose protonated nitrogen is
`NX4+`, so `[NX3;H2,H1]` did not match it — hydrazine·HCl beside H₂O₂ raised only `peroxide`, while
free hydrazine raised `hydrazine` and `oxidizer-with-reductant` too. The structural rule and the
pair-rule arm are widened together, because half of that fix reads exactly like a clean screen.
Neither prose moved, and neither had to: "free hydrazine motif … a strong reductant that forms
energetic mixtures with oxidizers" is about the N–N motif, not about its protonation state. Measured
across the 83 distinct structures of the reagent identity table, the widening newly matches four
hydrazinium salts and **nothing else** — ammonium, ethylenediamine, hydroxylamine, aniline and
guanidine salts all stay clean, and an acylated N–N is still excluded as a hydrazide.

Not fixed, for exactly the reason `peroxide-with-ketone` is not:
`complex-hydride-with-chlorinated-solvent` misses NaH (its explanation and its id both say *complex*
hydride, and sodium hydride is a saline one), and `azide-with-dichloromethane` misses chloroform
(its explanation names dichloromethane specifically, and triazidomethane from chloroform is a
different sentence). Both gaps are real and both need a chemist and a citation rather than a regex;
`BACKLOG.md` carries them with the measurement.

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

**The sibling tool needed the same verdict and was first shipped without one.** `calculator_outliers`
returned a bare `list[OutlierResidual]`, and with `calibration_enabled` at its default `False`,
`reconciled_for` returns `[]` without raising — so the shipped deployment answered `[]`, which this
tool's own docstring tells the model means "few measurements". Naming `Calibration`'s collapse and
leaving its neighbour's is half a fix. It now returns an `OutlierReport` carrying `enabled`,
`measured` (the ledger's size *before* `matching` filtered it) and the residuals, whose verdict
separates a disabled ledger from an unmeasured calculator from a class the ledger has never seen —
that last one because "no measured molecule contains `[Pt]`" and "the calculator handles platinum
well" are the same empty list to a chemist testing a hypothesis about a class.

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

The upsert now replaces both arrays **when the pass that produced them saw the whole corpus**, and
unions them otherwise. After the fix the probe above drops the retracted member, falls to support 2,
and leaves `promotable()` — proved through the real `mine_corpus` → `record` → `promotable` chain,
which is what the regression test now runs (it used to hand-write the payloads while its docstring
claimed the chain).

**The unconditional replace was itself an instance of this ADR's thesis, one layer down, and the
condition is the correction.** The safety argument given for it was that "both miners read the whole
corpus every pass (`all_reactions()` reads from `datetime.min`)". That is not a property the read
has: it reads the *currently enabled* sources and silently skips any entry `map_to_ord` rejects, so
a degraded pass rewrote evidence *down* and stated a partial reading as the complete record —
measured on live Postgres, a three-project observation reduced to support 1 by a pass that saw one
project, which can also knock a row out of `promotable()`.

So the read now reports what it is. `read_corpus()` returns a `CorpusRead` carrying `complete`, and
`record(observations, *, complete)` takes it keyword-only and without a default, because a caller
who has not thought about it is exactly the caller who must not get the authoritative branch by
accident. A complete pass replaces; a partial pass may add and may not delete, and leaves the
stored statement alone — rewriting a "one project" sentence beside three-project evidence is the
self-contradiction §6 exists to remove, arrived at from the other side. A retraction a partial pass
cannot distinguish from an invisible member simply waits for the next complete pass.

Completeness is about the read, not about configuration: a source an operator turned off is not part
of the corpus, so a read without it is complete and the evidence shrinking to match is the
documented consequence of the toggle. And a note `load_notes` skips does not make the pass partial
either — an unparseable note drops its whole observation from the batch, so no row is rewritten and
the row ages out through `retire_stale`, which is the designed path. What makes a pass partial is a
reaction the corpus holds and the miner never saw.

Two concurrent complete passes still last-writer-win, which is correct rather than tolerated: both
saw the whole corpus, so the later write is the more recent view of it.

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

## The two `tests/test_pka.py` "failures": a timeout, not a defect

They are **compute-bound timeout expiries against the repo-wide 180 s per-test cap**
(`pyproject.toml`), not failing assertions and not an environment difference in the tblite numerics.
`test_predicted_pkah_ranks_aromatic_bases_correctly` and
`test_in_sample_pkah_errors_are_far_below_the_acid_calibrations` each relax a dozen molecules
through GFN2, which takes ~90–180 s on an idle box and longer on a loaded one, so whether they
"fail" is a fact about the machine's load rather than about the code. Run with `--timeout=0` they
are **2 passed in 1071.49 s**. No assertion in either is ever evaluated when they expire.

This section previously recorded them as *refuted* — "27 passed on unaltered `main`" against
"29 passed with this change" — and concluded that the tblite-numerics hypothesis was "the one left
standing". Both readings were wrong in the same way: a pass and a timeout differ by how busy the
machine was, so neither run measured what it was taken to measure, and the hypothesis left standing
was one nobody needed. **Corrected here rather than in a new ADR because this ADR is the record of
that claim, and it is unmerged campaign work.**

What was correct, and is worth keeping: the pKa cache key could not have been their cause in any
case, because both tests call `predict_pka` directly and never touch a store. That is a sound
deduction about a question the timing answers anyway.

## Consequences

- Three campaign ids move once; see §5 and `BACKLOG.md`.
- Every `pka` and `xtb.opt` cache entry is invalidated (the key widened, which is what a key
  widening is for). `engine_version`'s docstring already states this is correct.
- **Every reconciled `pka` residual on file is orphaned with it**, so `calculator_trust("pka")`
  reports `UNCALIBRATED`, n=0, until each molecule is re-predicted (§2). Nothing needs re-measuring;
  the ledger refills per molecule, at the rate the calculator is used.
- **The `pka` cache is fleet-partitioned** under `xtb_engine=auto`: pods with and without the `xtb`
  binary compute different keys and each other's misses (§2). Pin `CHEMCLAW_XTB_ENGINE` to avoid it.
- `calculator_trust` and `calculator_outliers` now surface a database outage as an error instead of
  an empty ledger. That is a behaviour change for the two tools and no others.
- **`calculator_outliers` returns an `OutlierReport`, not a `list[OutlierResidual]`** — a payload
  shape change for that MCP tool. The rows are unchanged, under `residuals`, beside a verdict.
- `memory.observations.record` takes a required keyword-only `complete`. Its only production caller
  is the mining activity; a future caller must state whether its pass was authoritative (§6).
- Similarity search can now report `hits_truncated`, so a `top_k`-sized page is no longer presented
  as a total. No hit is added or removed; one extra row is read per search and dropped (§1).
- `--strict` can now fail for a reason other than a regression. `make eval` (non-strict) is
  unchanged.
- The `calc` connector resolves its calculator version at startup (`on_start`), so no request path
  is the first in a process to shell out to `xtb --version` on the event loop.

## Not fixed here

`peroxide-with-ketone`'s `[OX2H][OX2H]` misses `Na2O2 + acetone` for the same coordination reason
§3 fixes. Left alone deliberately: its explanation and citation are specifically about *hydrogen
peroxide* forming acetone peroxide, so widening the pattern without widening the prose would make
the explanation false for the molecules newly matched — and widening the prose is a chemistry claim
this change is not the place to make. Recorded in `BACKLOG.md` with the measurement and a trigger.

Its two neighbours are left on the identical argument and are recorded beside it: NaH is not covered
by `complex-hydride-with-chlorinated-solvent`'s prose, and chloroform is not covered by
`azide-with-dichloromethane`'s (§3). Each needs a citation and a rewritten explanation, which is a
chemistry claim, not a pattern edit.
