# D-100 — Sizing for real substrates: the workload is 200-800 Da

**Context.** The X3/X4 cost model was fitted on 3-14 atom test molecules. The actual target is
process R&D substrates in the 200-800 Da range, where conformer and job work runs in minutes,
not seconds.

**Measured, on this stack** (optimize + Hessian, one core):

| molecule                   | atoms | optimize (steps) | Hessian  | total   |
|----------------------------|-------|------------------|----------|---------|
| ibuprofen (MW 206)         |    33 |   11.6 s ( 71)   |   7.5 s  |  19 s   |
| sildenafil (MW 475)        |    63 |   66.0 s (154)   | 435.1 s  | 501 s   |
| atorvastatin core (MW 559) |    76 |   96.6 s (177)   | 218.3 s  | 315 s   |
| erythromycin (MW 734)      |   118 |  552.6 s (232)   |1007.1 s  |1560 s   |

**The old model predicted 47 s for the 76-atom case — under by a factor of seven**, and 100 s
for the 118-atom one against a measured 26 minutes. The exponent fitted on small molecules was
1.7; on real substrates it is ~3, because the fixed overhead that dominates a small molecule is
irrelevant at 76 atoms and the real scaling takes over.

**And atom count is not the whole story.** Sildenafil at 63 atoms costs *more* than the
atorvastatin core at 76 — its Hessian alone is twice as expensive — because a heteroatom-dense,
conjugated system carries more basis functions per atom and converges its SCF harder. No
function of atom count removes that scatter, so the refitted model (exponent 3.0, set to err
high) carries a factor of ~2 either way in the drug range. That is fine for its only job —
comparing against a threshold — and it is why the estimate reported to a user is an order of
magnitude, never a countdown.

**Consequences, all of them pointing the same way.** Everything in the target range now routes
to a durable job, which is correct rather than a limitation. `xtb_hessian_max_atoms` goes to 150
(an 800 Da molecule is ~120 atoms with hydrogens, so 120 was exactly at the ceiling);
`xtb_opt_max_steps` to 1500 (177 steps at 76 atoms, and the count grows with size, so 400 would
have failed large substrates *after* doing all the work); the job's start-to-close budget to four
hours. The activity now **heartbeats** between species, solvents and scan points through a
`Progress` callback (`calc/progress.py`), so a dead worker is detected in minutes rather than at
the four-hour timeout — and `calc/` still knows nothing about Temporal.

**A second finding, carried rather than fixed.** Sildenafil does **not** reach a clean minimum on
the first pass, so `relax_to_minimum`'s displacement-and-reoptimize loop is not a rare path at
drug size — and each attempt costs a full optimization *and* a full Hessian, which at 100 atoms
is tens of minutes. When the refinement triggers on a large molecule, it dominates the job. The
config comment says so; the reaction result already warns when a species is not a minimum.

**The bottleneck this exposes, recorded as X9 rather than fixed.** 177 Cartesian L-BFGS steps for
one 76-atom molecule (232 for 118 atoms) is the dominant cost, and it compounds through every scan point and every
species. A redundant-internal-coordinate optimizer typically cuts that 3-5x. The Cartesian
optimizer was the right first choice — dependency-free and easy to reason about — and it is now
the single largest speedup available for this workload.
