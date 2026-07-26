
## 2026-07-25 — Deep analysis pass (docs/audit/12)

- **Verify a "survived mutation" before calling it a coverage gap.** Two of five survivors were
  mis-targeted patches — one replaced a *docstring* occurrence of `created_by == "agent"` while the
  real guard at `kg/pr_gate.py:68` uses `!=`. Rule: after any mutation survives, read the patched
  line and confirm it is the invariant, then re-run the **full** suite (a narrow test file made two
  more look like gaps). A false finding costs more than a missed one.
- **Grep output truncated by `head` is not evidence of absence.** I nearly reported
  `service_allow_insecure` as dead config because the match scrolled past `head -10`; it is enforced
  at `service/app.py:447` and tested. Rule: before claiming "never read", grep that identifier alone
  with no pipeline.
- **A high complexity score is a question, not a verdict.** `create_app` scores 33 because mccabe
  sums nested route handlers in the FastAPI closure idiom — and that closure is what makes the app
  testable. Read the shape before proposing a refactor.
- **The file that exists only to be copied is the one never tested.** `.env.example` drifted into
  crashing the documented quickstart. Where a doc makes a checkable promise ("every field mirrored"),
  make it a test rather than restating it in prose.

## Do not assert a sampled quantity (2026-07-26, X6/CREST)

CREST is the first non-deterministic calculator in this codebase. My first test asserted
`total_found == 2` for n-butane — correct chemistry, and it passed twice. The third run
returned 4, because the metadynamics happened to split methyl-rotor variants differently.

**Rule:** before asserting anything about a stochastic calculator, ask "would this hold on a
different random seed?". Assert orderings, bounds, invariants (populations sum to 1) and
signs — never a count, never a population to three figures. A test that pins a sampled
quantity is a CI flake with a delay fuse, and it will fire on someone else's PR.

## A cost model fitted on toys does not transfer (2026-07-26, X3/X4)

I fitted the inline-vs-durable router on 3-14 atom test molecules, got an exponent of 1.7,
and shipped it. On the workload the system is actually pointed at (200-800 Da) it
under-predicted a 76-atom substrate **sevenfold** — the fixed overhead that dominates a small
molecule is irrelevant at real size, and the true scaling took over.

**Rule:** calibrate against the workload, not against the test fixtures. Before fitting any
performance model, measure at least one point at the top of the intended range. And when a
user states their workload ("MW 200-800", "minutes not seconds"), treat it as a
specification to re-verify against, not as context.

## Trust the output files, not the exit code (2026-07-26, X5)

`xtb --hess` on linear CO2 computes the Hessian correctly — the file holds its textbook
655/1345/2446 cm^-1 — and then aborts during teardown with SIGABRT. Treating exit != 0 as
failure would have silently lost every linear molecule.

**Rule:** for a subprocess backend, define success as "produced the outputs the task is
defined by", check that explicitly, and log when the exit code disagreed. The converse also
holds: exit 0 is not evidence the outputs exist.

## Two backends must agree on the physics, not just the interface (2026-07-26, X5)

`tblite` enables a spin-polarization term for open shells (without it, triplet O2 came out
*above* singlet). The `xtb` binary does not by default, and its `--spinpol` is OOM-killed in
this build. Dispatching a radical to whichever backend was configured would have silently
reintroduced the exact physics error D-085 removed.

**Rule:** when adding a second implementation behind one interface, enumerate what the first
one *fixes* and check the second does the same. Where it cannot, route around it explicitly
and make the cache key record which one actually ran.


## An optimizer change is worthless until it is timed (2026-07-26, X9)

The ANC preconditioner was *correct* on its first two attempts and useless on both: version one
ran every leg to the iteration cap (no stopping criterion), version two stopped every leg
immediately (threshold converted the wrong way). Both produced right answers. Only wall clock
showed one was 10x slower than doing nothing.

**Rule:** for anything whose purpose is speed, record the baseline *first* and compare against it
at every step. "It converges and the energy is right" is not evidence the change did its job —
and a performance change that is silently a regression is worse than not making it, because it
looks done.

## Sweep the constant that stands in for the missing physics (2026-07-26, X9)

I set the model Hessian's eigenvalue floor to 0.005 as a numerical safety net. It is not a safety
net: the pairwise model has no bend or torsion terms, so 37% of directions land on the floor and
that number *is* their assumed curvature. Measured against the true Hessian (median 0.40) and
swept, the optimum was 1.0 — two hundred times the "safe" value.

**Rule:** when a constant substitutes for physics the model omits, do not pick it for numerical
comfort. Measure what it is standing in for, and sweep it against the outcome it affects.


## Moving a capability tests every layer that names it (2026-07-26, X8)

Moving seven calculators from in-process to an MCP server broke three things that had nothing to
do with chemistry: the skill validator (resolved names against the in-process registry only),
agent profiles (`tool_names` could not name an MCP-hosted tool, and MCP attenuation was
server-granular), and an example script's imports.

Each was the same latent assumption — *a tool's name implies its transport* — and each fix was a
correction rather than an accommodation. The good sign afterwards: **no skill changed**.

**Rule:** before relocating a capability, grep for every layer that refers to it *by name*
(validators, profiles, registries, docs, examples) and ask whether that layer should care where it
lives. Usually it should not, and the migration is the moment that assumption becomes visible.


## A bad fit can be a real finding — split the class before recalibrating (2026-07-26, X11)

The task was "make basic amines work". The first fit over 20 amines gave R² 0.50 and ρ 0.28, and
the reflex was to reach for a better calibration. Splitting the set by nitrogen class instead gave
ρ **1.000** for aromatic/aryl N and ρ **−0.17** for aliphatic — one class the most accurate thing
in this system, the other carrying no information at all. A single fit had been averaging those
into a mediocre number that would have shipped for both.

The diagnosis is what made the refusal defensible rather than cautious: gas-phase GFN2 reproduces
the experimental proton affinity order exactly, ALPB reverses it, and the true aqueous order is
non-monotonic — so no linear recalibration can recover it, and the ceiling is the solvation model
rather than the fit. That also disproved the plan's stated route (a CREST protomer search), which
addresses structure and would not have touched a solvation failure.

**Rule:** when a calibration fits poorly across a chemical class, look for a sub-class boundary
before looking for a better functional form, and drive the failing half to a *mechanism* rather
than a residual. A refusal you can explain is a deliverable; a mediocre number for everything is
not. And check whether the diagnosis invalidates the plan's proposed fix before building it.

## Symmetric physics does not make a function symmetric (2026-07-26, X11)

`run_cached_interaction` keyed on a combined two-molecule structure, and its docstring asserted
that A-with-B and B-with-A "build the same arrangement" and share one cache entry. They do not:
`_combine` holds the first monomer at the origin and offsets the second along +x, so swapping the
arguments negates the intermolecular vector while leaving each monomer's own orientation alone —
not a rigid motion, so a different geometry and a different key. Two minutes-long searches for one
number. I had written the claim from the physics (the interaction of A and B is symmetric) rather
than from the code, and only a test that asserted it caught it.

**Rule:** when a quantity is symmetric in its inputs, do not assume the function computing it is.
Assert the invariant in a test at the cheapest layer that carries it — and if it fails, canonicalize
the input at the entry point rather than weakening the docstring to match the bug.
