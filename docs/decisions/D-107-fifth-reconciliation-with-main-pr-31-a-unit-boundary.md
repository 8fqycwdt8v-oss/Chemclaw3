# D-107 — Fifth reconciliation with `main` (PR #31): a unit boundary and a sign, both silent

`main` landed D-092's process/analytical calculators — logD, developability descriptors, a
reaction exotherm screen, a Boltzmann conformer ensemble — plus two CI fixes, while this
branch was under review. Seven files conflicted. The textual ones were routine. Two were
not, and neither would have failed a test on either branch alone: each is a defect that
exists **only in the combination**.

**The ADR numbers collided for the third time**, so this branch shifts again, D-092…D-103
to **D-095…D-106**. `main` keeps its allocation, as in D-105. This is now a recurring cost
of parallel branches rather than an accident, and `tests/test_decision_log.py` catches it
every time — which is the argument for the check existing, not against the practice.

### The unit boundary: `geometry()` returned different units on the two branches

X1 made `calc.xtb_engine` the **single unit boundary** — Angstrom above it, Bohr only
inside, conversion in `make_calculator`/`evaluate_point`. `main` never had that change, so
its `geometry()` returns Bohr and its `gfn2_energy` consumes Bohr, which is self-consistent
*there*. It also added `positions_bohr`, a genuinely useful helper for reading one conformer
of a multi-conformer embedding by id, which `calc.conformer_ensemble` feeds straight into
`gfn2_energy`.

Merged naively, that helper hands Bohr to a function which on this branch multiplies by
1.8897 — every ensemble geometry inflated by that factor, energies wrong and entirely
plausible. Resolved by keeping the boundary and renaming the helper to
`conformer_positions`, returning Angstrom: the name now states the unit, which is what stops
the next person reintroducing it. Pinned by a test that asserts water's O-H is ~0.96 and not
~1.81 — two numbers no one can confuse.

### The sign: logD took the acid form for a base

`calc.logd` composes `predict_logd` from Crippen LogP and `calc.pka` via
Henderson-Hasselbalch, and hard-coded `logD = clogP - log10(1 + 10**(pH - pKa))`. That is the
**acid** form, and it was correct when written: `calc.pka` covered acids only and *raised*
for a base.

X11 widened `calc.pka` to aromatic and aryl nitrogen. Pyridine stopped raising and started
flowing into the acid formula, where the ionized fraction rises with pH instead of falling.
Measured: pyridine at pH 7.4 came out at **-0.92 against a clogP of 1.08** — two full log
units too lipophobic, for a base that is >99% neutral at that pH, and nothing raised.
`predict_logd` now branches on `PkaResult.site`, which is the field that makes the two
distinguishable and the reason it exists.

The general lesson is worth more than the fix: **widening a domain is a breaking change to
every consumer that encoded the old one**, even though nothing about its signature changed.
`calc.pka` gained a capability; `calc.logd` silently lost its correctness.

### Two implementations of two capabilities now coexist, deliberately

`calc.conformer_ensemble` (RDKit ETKDG + MMFF prune + GFN2 single points) alongside
`calc.conformers` (CREST metadynamics, rotamer degeneracies, conformational entropy); and
`calc.reaction_energy` (cached single points, stoichiometric coefficients, exotherm flag)
alongside `calc.reaction` (optimizes every species, Hessians, ΔH/ΔG, balance enforced).

Both pairs are kept, and neither was deleted, because the choice is a product decision rather
than a merge decision. They are also genuinely different: the CREST search needs an optional
binary and costs minutes; the ETKDG ensemble is dependency-free and always available. The
exotherm screen is a hazard flag on unoptimized geometries; the reaction composite is a
thermodynamic answer that refuses an unbalanced equation. The tool names do not collide, so
the registry is satisfied. **What is owed is a decision, not a merge**: `BACKLOG.md` carries
it as an open item rather than this ADR pretending it was resolved.
