# D-2026-08-01-a-key-names-what-ran — A calculation key names every program that produced it

**Status:** accepted · **Date:** 2026-08-01 · **Extends:** D-011 (a result is persisted once and
never recomputed) · **Supersedes:** D-106 §3, on one point only — where the `engine` exclusion
belongs. Everything else in D-106 stands. · **Companion:**
D-2026-08-01-a-key-that-cannot-see-our-own-fix (the other axis, `CALCULATION_EPOCH`)

## Context — the apparent contradiction

The calc layer held two rules that read as opposites.

**The backend belongs in the key.** `XtbSpec.calc_version` folds the resolved engine and its
build into `calc_version` — "a key that named the wrong program is a key that survives an
upgrade to the right one" (D-011). `connectors/qm/cache.py` applies the same reasoning to the
DFT key, so a `mock` energy synthesized from a job id's hex digits cannot be served to a
deployment pointed at a real cluster.

**The backend does not belong in the key.** D-106 dropped `engine` from `CrestSpec.calc_version`,
and `tests/test_xtb_cli.py` pinned it — negatively, asserting the engine build is *absent* from
the version string of both CREST-backed specs.

## What D-106 actually argued

Not "backends do not matter", and not "a stochastic search need not be keyed on its backend".
Its argument (§2 and §3 together) was narrower and correct: **`engine` named a program that
never ran.** `compute_ensemble` calls `crest_cli.run` whatever `engine` says, and
`XtbSpec.for_structure` rewrites it to `tblite` for any open shell — so a radical's ensemble was
keyed as tblite's while crest did the work, and a crest upgrade moved no key at all. D-106
replaced a false name with a true one.

That is **the same rule as D-011**, not its opposite. Both say: name what ran.

## Where D-106 went wrong

It placed the exclusion on `CrestSpec`, the base class, and in the same edit made its own
premise false for one subclass. D-106 §3's last sentence reads: "`ComplexSpec` additionally
propagates its engine into the `OptSpec` its three optimizations use, which they previously
re-resolved independently."

For `ComplexSpec`, `engine` is not a dead inherited field. Crest only *chooses the binding mode*.
Every number `InteractionResult` reports comes out of the three `optimize_structure` calls in
`compute_interaction` — `interaction_energy_kcal` is `bound.energy_hartree` minus the two
monomer energies, and the reported `structure` is the optimizer's geometry. `xtb_opt` keys its
*own* rows on the backend and says why: "the two are separately cached because they do not
produce identical geometries." A composite that omitted it therefore let a Cartesian-L-BFGS
interaction energy and an ANCopt one share a cache entry — on a *difference* of three
relaxations, where the measured per-species agreement tolerance (2e-3 Hartree in
`test_both_backends_reach_the_same_minimum`, ~1.3 kcal/mol) is already comparable to a weak
complex's entire binding energy. The methane dimer this module tests against binds by
0.5 kcal/mol.

`ConformerSpec` is the case D-106 examined, and there the drop is right: crest produces every
number in a `ConformerEnsemble`.

## Decision

**`calc_version` names every program whose output survives into the stored payload, and no
program that does not run.** Argued from what a cached number means to whoever reads it later:
a stored row is a claim that *these programs, at these builds, produced this number*. A name
that was not there when the number was made is a lie a reader cannot detect; a name that is
missing is a collision a reader cannot detect either. Both are the same defect approached from
opposite sides, which is why one rule covers them.

Applied:

- `XtbSpec` — runs on `engine`, names it. Unchanged.
- `CrestSpec` — runs on crest, names crest, drops `engine`. Unchanged; D-106's finding holds
  for a spec whose numbers are all crest's.
- `ComplexSpec` — runs both, so `calc_version` now appends `engine` and its build to crest's.
  This is the change.
- `connectors/qm` — `mock` and `nextflow` are different producing programs, so the backend
  stays in `calc_version`. Unchanged; the fix that landed this session is correct under this
  rule and is not retracted.

The backend goes in `calc_version` rather than in `params` for the reason `XtbSpec` already
gives — a backend is a program, not a knob — and for a second: `calc_version` is also the
calibration ledger's key (`calibration.calibration_for`), and residuals measured against
ANCopt-relaxed complexes must not reconcile against Cartesian-L-BFGS ones.

## Why this is not `CALCULATION_EPOCH`

`CALCULATION_EPOCH` arrived the same day (D-2026-08-01-a-key-that-cannot-see-our-own-fix) and
covers "what a stored result means changed on *our* side". Whether one mechanism should carry
both questions was asked deliberately; the answer is no — they are different axes, and that ADR
already names the difference in its consequences: "the epoch changes per release, not per
deployment."

`CALCULATION_EPOCH` is a **source constant**. It moves once per release, by a human judging that
a ChemClaw-side fix or payload change made already-written rows wrong, and it invalidates every
deployment at the same instant.

A backend is **configuration**. Two deployments running the byte-identical release resolve
different ones — `settings.xtb_engine` may say `auto`, which is precisely why `resolve_backend`
refuses to let the string `auto` reach a key. The partition it creates is between *deployments
at one release*, not between *releases*.

Merging them fails in both directions. Folding a backend into the epoch would make switching a
backend a source edit and a release — impossible for `auto`, which resolves per host. Folding
the epoch into a version string would make a ChemClaw-side fix invisible wherever the underlying
programs happened not to move, which is the exact failure the epoch was introduced for (the
linear-rotor thermochemistry bug moved no `xtb.hess` version at all).

No epoch bump accompanies this ADR. `ComplexSpec` rows written before it become unreachable
because their `calc_version` no longer matches — that is the mechanism doing its job, not a
ChemClaw-side meaning change, and bumping the epoch would additionally throw away every
unrelated calculator's cache for no reason.

## What the pinning test asserted, and what it asserts now

`tests/test_xtb_cli.py::test_crest_backed_specs_are_keyed_on_crests_own_build` looped over
`(ConformerSpec(), ComplexSpec())` and asserted, for each: `crest_cli.binary_version() in
version` and `backend_version("tblite") not in version`. The second assertion is the one that
pinned the losing side, and it is now made for `ConformerSpec` only. The positive crest
assertion still covers both.

`test_a_complex_key_names_both_programs_that_produced_it` replaces it for `ComplexSpec`: both
builds appear in the version string, two engines produce different keys, and — as for `XtbSpec`
— `params_hash` is *equal* across the two, because this is a version split and not a parameter.

`test_the_three_optimizations_run_on_the_backend_the_key_names` pins the propagation the new
key claim depends on: `_opt_spec` carries `engine` across rather than letting `OptSpec`
re-resolve it, without which the key would name a backend that did not relax anything.
