
## 2026-07-25 — Deep analysis pass (docs/archive/audit/12)

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
reintroduced the exact physics error D-098 removed.

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


## The docstring is the best bug detector in a codebase that writes them (2026-07-27, review)

A heavy review of 12k green lines found five real defects, and **three announced themselves
in their own docstring**. `_energy_and_gradient` said "for GFN-FF the check is skipped" and
did not skip it. `crest_cli.binary_version` said "for the cache key" and no cache key
called it. `crest_cli.run` said "lowest energy first" and never sorted. In every case the
prose was right about the intent and the code had drifted from it — so the fastest read was
not "what does this do" but "does this do what it says".

That works here *because* the house style demands a why-docstring on everything. It is the
compounding return on that rule: the docstring is a second, independent statement of intent,
and a diff between two statements of intent is exactly what a reviewer can spot and a test
usually cannot.

**Rule:** when reviewing, read each docstring as an assertion and check it against the body.
Treat a mismatch as a defect in the *code* until proven otherwise. And when writing, never
soften a docstring to match a body you have not verified — that converts the detector into
camouflage.

## Green tests prove the paths you thought of (2026-07-27, review)

Every defect above sat in tested code. GFN-FF was tested at the `xtb_cli.run` layer and
never through `optimize_structure`, the layer that broke it. The CREST cache key was tested
for *hits* (same input, served from store) and never for *misses* (upgraded binary, must
recompute) — and a cache bug is always on the miss side, because a key that is too coarse
still hits. `conformer_treatment` had a `Literal` type that made the wrong value the only
possible value, so no assertion could have failed.

**Rule:** for a cache, test that the key *changes* when each versioned input changes, not
only that it repeats. For a layered capability, test it through the entry point callers
actually use, not the layer it is implemented in. And treat a single-value `Literal` as a
smell: a field that cannot vary cannot be right except by luck.


## Widening a domain breaks every consumer that encoded the old one (2026-07-27, PR #31 merge)

X11 widened `calc.pka` from acids to acids-plus-aromatic-bases. Nothing about its signature
changed, nothing in its own tests broke, and it was strictly more capable. On another branch,
`calc.logd` had hard-coded the **acid** Henderson-Hasselbalch form — correct when written,
because `calc.pka` *raised* for a base. Merged, pyridine stopped raising, flowed into the
acid formula, and came out two log units too lipophobic with nothing raising.

Neither branch's tests could have caught it: the defect exists only in the combination. And
it is not a merge-conflict class of problem — the two files never touched the same lines.

**Rule:** when a function's *domain* widens, grep for its callers and check each one for an
encoded assumption about the old domain — a hard-coded sign, a branch that is now reachable,
an error path that no longer fires. Prefer widening behind a discriminator the caller must
read (`PkaResult.site`) over widening silently, and treat "it used to raise here" as a
contract that consumers were entitled to rely on.

## Two branches, one architecture rule — check the invariants, not just the lines (2026-07-27)

The same merge nearly inflated every conformer geometry by 1.8897. `main`'s `geometry()`
returned Bohr; this branch had made `calc.xtb_engine` the single unit boundary and returned
Angstrom. Both self-consistent, neither wrong, and git merged the *new helper* cleanly
because no line collided — it just pointed at the other branch's convention.

**Rule:** after a merge that spans an architectural change, list the invariants that change
introduced (units, ownership of a cache key, who resolves identity) and check the incoming
code against each one directly. A clean textual merge says nothing about them. Where the
invariant is a unit, put it in the *name* — `positions_bohr` vs `conformer_positions` — so
the next reader cannot get it wrong silently.

## A test double's signature is untyped, and untested where it cannot run (2026-07-28)

`propose_note` gained a `dependencies` argument (D-133). One test stubbed it —
`tests/test_connector_job_workflow.py` — with a hand-written `_fake_propose(note, _submitter)`.
The stub then raised `TypeError` inside the activity, the note was never published, and the
end-to-end assertion failed as `[] == ['fixture-benzene']`.

Two guards that should have caught it did not, for the same underlying reason each time:

- **`mypy --strict` cannot see it.** `monkeypatch.setattr("module.propose_note", stub)` replaces
  an attribute by string name; there is no typed edge between the stub and the function it
  stands in for, so a signature mismatch is invisible to static checking.
- **The suite could not run it.** That test needs a Temporal server, which the offline sandbox
  cannot fetch, so it skipped on *every* local run — 18 Temporal tests and 38 Postgres tests
  skip here. Local green meant nothing about them, and I reported "green" without qualifying it.

I merged the regression because I read `make lint type test` passing as proof, when the only
authority for those 56 tests is CI. I had even written "migration 019 has never actually run" in
the PR body — the same class of gap — without drawing the general conclusion.

**Rules:**

1. **When changing a function's signature, grep for stubs of it**, not just callers:
   `git grep -n 'setattr.*<name>"' && git grep -n 'def _fake_<name>'`. A stub is a caller that
   type-checking cannot see.
2. **Bind a stub against the real signature** rather than restating it:
   `inspect.signature(real).bind(*args, **kwargs)`. The double then accepts exactly what the real
   function accepts, and drift becomes impossible rather than merely detectable.
3. **When a behaviour's only test is infrastructure-gated, add a sandbox-safe sibling.** This
   file's own precedent is `test_the_wrapper_is_served_by_the_background_worker`, whose docstring
   states the argument outright. Calling the activity directly with its one dependency stubbed
   costs four lines and runs everywhere.
4. **Never report a suite as green without naming what skipped.** "1377 passed, 69 skipped" is
   only a claim about 1377 tests. Say which subsystems the skips cover and that CI is their only
   verification.

---

## A configuration only production sets is a configuration nothing tests (2026-07-30, D-152)

`CHEMCLAW_HARNESS_ENABLED=true` ships in the Helm chart. The code default is `False`, and every
test in the suite runs at the default. So the harness middleware stack had **2066 green tests and
zero executions of the path production actually runs**. The first live turn under it crashed before
reaching the model: `ToolApprovalMiddleware requires an AgentSession`. `make chat` and
`uv run chemclaw` — the documented testing seam — could not take a single turn under the shipped
configuration, and had not been able to for as long as the flag has been in the chart.

This is the *third* instance of the same shape in this repo's record: LIVE-1 (`ScriptedChatClient`
derived from the middleware-free base, so every harness test ran a pipeline with no chat
middleware), the three original review Criticals (all in code paths gated behind a flag nothing
enabled), and now this. Every time, the tests were green about a different program than the one
that ships.

What made it findable was not cleverness — it was running the real entrypoint under the real
configuration, once. Ten minutes.

**Rules:**

1. **Diff the shipped configuration against the test configuration, and treat every difference as
   untested.** Concretely: `grep` the Helm `values.yaml` for every `CHEMCLAW_*` it sets, and for
   each one check whether any test sets it too. A flag production turns on and tests leave off is
   not "covered by the default path"; it is a second program.
2. **A feature flag's *on* state needs at least one test, even a construction-only one.** Building
   the agent under `harness_enabled=true` and asserting it can take one stubbed turn would have
   caught this without a credential and without a network call.
3. **When a flag cannot be tested offline, say so where the flag is defined**, not in a backlog
   entry — the next person to read the setting is who needs to know its *on* state is unverified.
