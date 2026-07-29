# D-102 — X9 revisited: preconditioning the path the binary cannot take

**Context.** D-101 retired X9 on the grounds that ANCopt *is* the internal-coordinate optimizer
and it is a process call away. That was right about the general case and wrong about the scope:
two paths cannot use the binary at all, and neither is rare.

- **Relaxed scans.** Holding atoms fixed is expressible as optimizer bounds but not as an xtb
  flag without writing a control file — precisely the input surface `calc.xtb_cli` refuses to
  have. A scan is one constrained optimization *per point*, so a 24-point profile pays the
  Cartesian cost 24 times.
- **Open-shell species**, which route to the in-process backend because the binary cannot apply
  the spin-polarization term their energy needs.

So the work was never "replace ANCopt"; it was "stop the fallback path — which handles exactly
what ANCopt cannot — from being the slow one".

**Decision.** Optimize in the eigenbasis of an approximate Hessian, scaled by the square root of
its curvature (`calc/anc.py`). The transform is **linear**, so a step is an exact Cartesian
displacement and there is nothing to back-transform — the same reason xtb's own optimizer uses
approximate normal coordinates rather than redundant internals. Frozen atoms are excluded from
the basis by construction, so the constraint is not something the optimizer can violate.

### Three things that only measurement decided

**The first version was 10x slower than no preconditioner at all.** Setting L-BFGS-B's `gtol` to
zero left it with no stopping criterion, so every leg ran to `maxiter`. The second attempt
converted the threshold into preconditioned units using the *softest* direction's scale — the
wrong end — and every leg then stopped almost immediately, failing to converge in 1500 steps. The
fix is to stop on the quantity actually promised: the objective records the Cartesian gradient
and a callback halts the leg when it meets the tolerance, so no threshold is ever converted
between unit systems.

**The eigenvalue floor is not a safety net — it is the model.** Lindh's pairwise form has no
bending or torsional terms, and on ibuprofen that leaves **37 of 99 directions with essentially
zero curvature**, where the true Hessian's lower quartile is 0.089 and its median 0.40
Hartree/Angstrom^2. The floor is the stand-in for what the model cannot see. At a safety-net
0.005 the preconditioner was *slower* than none; swept against measured step counts it optimizes
near 1.0 and turns over by 1.5.

**The payoff, at floor 1.0:**

| case                      | Cartesian | preconditioned |
|---------------------------|-----------|----------------|
| naproxen                  |  44 steps |  19 steps      |
| ibuprofen                 |  71 steps |  24 steps      |
| ibuprofen, 2 atoms frozen |  57 steps |  27 steps      |
| benzyl radical            |  10 steps |   6 steps      |

About **2x**, consistently, including both cases this exists for. Stated honestly rather than
sold: that is modest beside ANCopt's 8-11x, and the reason is visible in the number — with the
floor this high the scale ratio is only ~3, so the model is damping the stiff directions it
identifies reliably and is trusted for nothing else. A full Lindh model with angle and torsion
terms would do better, at the cost of primitive-internal machinery and a Wilson B matrix. Worth
it only if scans and radicals ever become the common case; recorded rather than built.
