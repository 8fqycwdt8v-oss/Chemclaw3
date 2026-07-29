# D-106 — Heavy review of the xTB layer: five defects the tests did not catch

A full read of the branch's 12k lines against `main`. The green suite was not evidence:
every defect below sits in a path the tests exercised from the wrong side, and three of
them were **contradicted by their own docstring**, which turned out to be the most
reliable place to look. Where a module said what it did and the code did something else,
the docstring was right about the intent and the code was wrong.

### 1. GFN-FF optimization could never succeed

`_energy_and_gradient` substituted GFN2 for GFN-FF and `_optimize_with_binary` then
checked the result against this module's Cartesian gradient tolerance. A converged
force-field geometry is not a GFN2 stationary point: measured on octane, GFN2 max-gradient
**1.3e-2** against a 5e-4 target, so every GFN-FF optimization raised "did not converge".
Had one passed, its `energy_hartree` would have been a GFN2 number labelled GFN-FF.

The whole large-system escape valve — the 118-atom substrate in 0.7 s that justifies
carrying GFN-FF at all — was unreachable through `optimize_structure`, and reachable by a
model through `run_xtb_task(task="opt", method="GFN-FF")`. The docstring already described
the correct behaviour ("for it the check is skipped and xtb's own convergence stands"); it
is now implemented. `max_gradient` is `float | None`, `None` for GFN-FF only, and
convergence there is xtb's own "CONVERGED AFTER" — required, not inferred from an exit
code the module elsewhere documents as unreliable. Widening the type made `mypy` name both
call sites, which is the argument for widening it rather than returning a sentinel.

### 2. A CREST upgrade served stale ensembles

`calc_version` named the tblite/xtb build for *every* spec, including `ConformerSpec` and
`ComplexSpec`, whose work crest does. `crest_cli.binary_version()` existed and its
docstring read "for the cache key (an upgrade must recompute)" — and nothing ever called
it. So upgrading crest, the program that produced the number, changed no key and every
stored ensemble and interaction energy survived it. A dead function whose docstring
asserts a guarantee is worse than an absent one; it reads as implemented.

### 3. `engine` was inherited by two specs that never honour it

`compute_ensemble` and `compute_interaction` call `crest_cli.run` whatever `engine` says.
`XtbSpec.for_structure` rewrites `engine` to `tblite` for any open shell — so a radical's
ensemble was keyed as tblite's while crest did the work.

Both are fixed by one seam: `calc_version()` is now an overridable method, and `CrestSpec`
overrides it to key on crest's build and drops `engine` from the key entirely, with
`for_structure` a no-op. The honest consequence is now *stated* rather than hidden: an
open-shell CREST search gets no D-098 spin-polarization fallback, because there is nowhere
to fall back to. `ComplexSpec` additionally propagates its engine into the `OptSpec` its
three optimizations use, which they previously re-resolved independently.

### 4. The open-shell caveat was gated on one level

`if level == "standard" and any(multiplicity > 1)` — so a homolysis run at `thorough`, the
most expensive path, lost the warning that unrestricted GFN2 energies are an ordering
rather than a value. The caveat is about the energies, which every level differences.

### 5. Two fields that could not tell the truth

`conformer_treatment: Literal["single"] = "single"` was structurally incapable of
reporting the ensemble treatment, and was therefore wrong at exactly `thorough`, the one
level where a reader needs it. And `conformational_entropy_kcal=round(x, 3) or None` sent
a rigid species' genuine 0.000 to `None`, which means "not computed at this level" — a
different claim.

### Two smaller ones, and one calibration note left open

`crest_cli.run` promised "lowest energy first" and returned file order; crest does sort, but
`ConformerEnsemble.lowest` is `conformers[0]` *after* a truncation to `max_members`, so the
unenforced invariant would have silently dropped and misreported the lowest conformer. Now
sorted. `xtb_cli._safe` was applied to the solvent but not to `xtb_cli_opt_level`, against
the module's stated rule that every argv value is checked — operator-supplied rather than
model-supplied, so not a boundary breach, but a rule with a quiet exception is not a rule.

**Left open, deliberately:** `ensemble_seconds` has no fixed-overhead term, so it predicts
0.5 s for a water CREST search that really takes ~10 s, and small searches route inline.
It is the mirror of the error the cost model fixed at the large end. Recorded rather than
re-fitted, because fixing it properly means measuring CREST's startup across sizes, which
is a measurement session and not a review edit.
