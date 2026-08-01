
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

---

## Prose is not covered by any gate, and it makes claims that a test would refuse (D-156)

A consistency pass over the repository structure found three defects. None were in code, all three
were the same shape, and every one of them had survived a full `make lint type test` plus eight
validators:

- `deploy/README.md` documented an `mcp-molfp`/`mcp-rxnfp` component that `entrypoint.sh` has no
  case for and the Helm chart has never declared. This is **D-117 verbatim** — a document asserting
  a deployable that does not exist — and D-117's own fix (`tests/test_deploy_chart.py`, chart ↔
  entrypoint in both directions) does not read README files, so the check that exists for exactly
  this failure could not see it.
- `src/chemclaw/mcp/README.md` stated the directory *"cannot be named `mcp`"* while sitting in a
  directory named `mcp`. The rule was true of a **top-level** package shadowing the SDK, and D-148
  had made it a submodule where it never applied. The rule outlived its condition and stayed
  quotable.
- `tests/test_deploy_chart.py` quoted a historical entrypoint line, and D-148's repository-wide
  rewrite of `mcp_servers.…` paths had edited the *quotation*, so it recorded something the file had
  never said.

The common cause is not carelessness. Code that goes stale fails; **prose that goes stale gets
believed**, and the more carefully it is written the longer it is believed.

**Rules:**

1. **When moving or deleting a thing, grep for its name in prose, not only in code.** The import
   rewrite is the easy half and the tooling does it. `git grep -n '<oldname>' -- '*.md'` is the half
   nothing does for you.
2. **A mechanical substitution cannot tell a claim about the present from a quotation of the past.**
   Scope it to files this branch authored, or read the whole diff before committing. This has now
   caused two defects: D-148 corrupted other branches' ADR citations by renumbering repository-wide,
   and edited a quoted historical path in a test docstring.
3. **Never write a rule as an absolute without naming its condition.** "Cannot be named `mcp`"
   should have been "a *top-level* `mcp/` shadows the SDK". The unconditional form is what survived
   into a context where it was false.
4. **When a document asserts something structural, ask what would fail if it were wrong.** If the
   answer is "nothing", either add the check or write the claim as a pointer to the thing that is
   checked. `ARCHITECTURE.md` promised for two restructures to stay in sync with the tree; D-156
   made `tests/test_repo_map.py` enforce it in both directions instead.

---

## A conflict-marker scan that matched an exact width exempted the conflicts it most needed to catch

Resolving a rename-heavy merge, the sweep for leftover markers was
`grep '^<<<<<<< \|^>>>>>>> '` — seven characters and a space. Git writes **eight** for a
rename/rename conflict, because the marker carries the two paths (`<<<<<<<< HEAD:old/path.md`). So
the one file that was still full of conflict markers reported clean, and it was the ADR — the single
file where a corrupted heading breaks the identity the whole `docs/decisions/` mechanism rests on.

`tests/test_decision_log.py::test_every_filename_matches_its_heading` caught it, by noticing the
file carried two `# D-NNN` headings. That is the check doing exactly its job, and it should not have
had to.

The shape is familiar enough to be worth naming: **a validator written against one example of a
pattern silently exempts the variants**. Same family as a `glob` over a moved directory returning
empty and a discovery loop iterating a set that is now empty — the check runs, finds nothing, and
reports success.

**Rules:**

1. **Match conflict markers by class, not by width**: `^(<{4,8}|>{4,8})[ <>]|^={4,8}$`. Rename and
   submodule conflicts do not use the seven-character form.
2. **When a hand-rolled scan and a test disagree, the scan is wrong.** The test asserts a property;
   the scan asserts a spelling. Fix the scan and keep both.
3. **After `git checkout --ours <file>` on a rename/rename conflict, read the file.** `--ours` there
   resolves *which path* wins, not which content — the result can still contain markers.

## A test that skips is not a test that passes — and an infrastructure skip can often be removed (2026-07-31, D-157)

The Temporal-backed and Postgres-backed suites here skip in the sandbox, which is documented and
accepted. Working on D-157 I first wrote the write-path assertions into the Temporal e2e test and
moved on, and the honest state of that work was: *the property is unasserted anywhere I can run*.
Two different corrections came out of it.

**The Postgres half did not have to skip at all.** `postgresql-16` was installed in the sandbox the
whole time; the only thing missing was pgvector (an `apt-get install`, then a source build when the
packaged 0.6.0 predated `bit_jaccard_ops`). Twenty minutes turned four skipped store tests into four
that ran — and they ran against the real migration, which is the thing a hand-checked `INSERT`
cannot verify. "The offline sandbox has none" was true of the *default* environment, not of the
environment I was allowed to build.

**The Temporal half genuinely cannot run** (`temporal.download` is blocked by the network policy),
so the fix was structural: pull the pure part out — `job_record_for`, a plain function from a run to
its record — and pin it offline, leaving only the orchestration to CI. That is the same move
`completed_job_status` made for D-153, and the test it makes possible is the one that would actually
catch a wrong field.

What is left unasserted offline is now *known*: core applying the provenance footer is only
exercised in the CI-only e2e run. I checked that by mutating the source and watching the offline
suite stay green, rather than by assuming.

**Rules:**

1. **Before accepting an environment skip, try to remove it.** Check what is actually installed
   (`which`, the package manager) rather than trusting the skip message; a skip reason written when
   the sandbox was different is not a current fact.
2. **When a path truly cannot run locally, extract the pure part and test that.** "It needs a
   server" is usually true of the orchestration and false of the mapping the orchestration performs.
3. **Verify a coverage claim by mutation, not by reading.** Break the line, run the suite, and see
   which test fails. Every "verified to fail without the fix" claim in a review section must be one
   you actually ran — I wrote one in `tasks/todo.md` before running it, and it was wrong.

## The obvious implementation fails silently (2026-07-31, dataflow plan W2–W3)

Five workstream items, and **three had an "obvious" implementation that was worse than the shipped
one in the same way**: it would have produced a silently wrong or silently empty answer rather than
a visible failure. The pattern is worth naming because it recurred across unrelated subsystems and
I nearly shipped it twice.

- **A filter that cannot be satisfied returned `[]`.** A molecule filter on the xTB task family
  (keyed by 3-D structure, not by molecule) would have answered "nothing found" for a question that
  cannot be asked that way — and `find_calculations` exists to be trusted when it says nothing was
  computed, so a chemist would have gone and recomputed hours of DFT.
- **A cap truncated deterministic output.** `notes[:cap]` on a memory job that rescans the whole
  corpus every night proposes the same first N forever and the tail *never*. A visible PR flood
  traded for silently lost knowledge, with a log line as the only trace.
- **A two-branch conditional answered every third case wrongly.** `if property_name ==
  "solubility"` plus two ternaries meant `logd` got a confident, well-formed calibration report
  about pKa's calculator, in pKa's unit.

They are the same shape: a code path that *cannot* serve the request answers as though it had, in
the request's own vocabulary, so nothing downstream can tell. The fix each time was to make the
impossible case loud — refuse and name the alternative, rotate rather than truncate, raise and list
what exists — which cost a few lines and no design complexity.

**Rules:**

1. **For every branch that returns an empty/default result, ask what a caller would conclude from
   it.** If "there is none" and "you cannot ask that here" are indistinguishable in the return
   value, they must be distinguishable in the *type* of the response: refuse one of them.
2. **A conditional over a closed set of names is a lookup table with a bug in the `else`.** Two
   cases is already enough — the wrongness of the third is not caught by any test that only
   exercises the two.
3. **Before adding a cap, ask what the input looks like on the next run.** A cap over deterministic
   input is not a cap, it is a permanent filter.

## A test that builds its fixture through the code it tests cannot fail (2026-07-31, v1 readiness)

Four tests in one branch passed with their own fix removed. The rule above ("verify by mutation")
caught them, so the rule works — what it did not do is explain *why* they were written that way,
and three of the four shared one cause worth naming separately.

- **The fixture came from the function under test.** To check that a v1 audit row still verifies
  after the chain was versioned, I built the v1 rows by calling `chain_hash(..., version=1)` — the
  very function whose version switch was the fix. Delete the switch and both sides move together.
  The rewrite reimplements the v1 payload independently, in the test, from the migration's own
  column list; it is duplication, and it is the only thing that makes the assertion mean anything.
- **The fixture was too well-behaved to discriminate.** A sort-key test built from `D-009`/`D-010`
  passes against a deliberately flattened key, because zero-padded ids make lexicographic order
  numeric order. Only `D-900` breaks it, because `'9' > '2'`.
- **The mutation itself was partial.** One re-run `sed`-ed a `return` that appeared twice and
  mutated one branch, so the suite failed for the wrong reason and I nearly recorded it as proof.

**Rules:**

1. **Never construct a test's expected value with the function under test, or with anything that
   shares its fix.** If the only honest fixture is a hand-rolled reimplementation, write it — a
   test that duplicates ten lines of logic is cheap; one that tautologically agrees is worse than
   absent.
2. **Choose fixtures where the naive implementation gives the wrong answer.** If the case passes
   under both the fix and its absence, it is documentation, not a test. Pick the value that
   separates them and say in the docstring why that value.
3. **After mutating, read the failure, not just the exit code.** Confirm the failing test is the
   one meant to fail and that it fails for the mutated reason.

## Three of my fixes were rebuilt by other sessions while I built them (2026-07-31, v1 readiness)

Concurrent sessions do not only collide on ADR *numbers* — they collide on *work*. Over one branch,
three substantive fixes (note types, plan-approval binding, job rationale) were independently found
and merged to `main` by other sessions while mine were in flight. In two of the three, theirs was
better: main's D-167 consulted the durable approval store where mine compared an in-process value,
and main's D-164 deleted two dead note types where I had added them to the schema. The dated-id
scheme fixes name clashes and does nothing about this.

The cost is real on both sides, and the merge is the wrong place to discover it: by then the choice
is between reverting someone's merged decision inside a merge commit and throwing away a day.

**Rules:**

1. **Before implementing a finding, grep `origin/main` for the defect, not just for your branch's
   base.** A one-line `git log origin/main --oneline -20` and a grep for the symbol costs seconds
   and is the whole check.
2. **Re-fetch `origin/main` before every push, not only when a merge conflicts.** A clean
   three-way merge is not evidence that nobody solved the same problem differently.
3. **When both fixes exist, compare them on the merits and defer to the merged one unless yours is
   clearly better.** Say which and why in the PR — "theirs survives an eviction, mine did not" is
   the useful record, and re-adding what another session deliberately deleted is never a merge
   commit's job.

## Writing the rule is not applying it (2026-07-31, reviewing my own W2/W3 diff)

I committed "the obvious implementation fails silently" to this file, and then — asked to review
the same branch's diff — found four defects in it, **two of which are that exact shape**:

- a promotion that minted `[[reaction-interaction-42]]`, a citation to a note that cannot exist,
  failing `kg-validate` on the PR it had just opened, after marking the source observation
  `promoted` so nothing would retry it;
- an observation id hashed from a statement containing mutable counts, so a cluster gaining a
  member minted a *second* row instead of accumulating onto the first — silently defeating the
  support mechanism the whole tier's promotion rule rests on, and leaving two rows contradicting
  each other in the retrieval bucket for the retirement window.

The second one is the sharper lesson: `memory/ids.py` documents that exact failure for note ids
("hashing the exact set would mint a brand-new id whenever a cluster gains a member"), I read that
file while building the tier, and I reproduced the bug anyway. Knowing a rule and checking my own
code against it are separate acts, and I only performed the second when asked to.

Both were found in minutes by *running* the code — building the object the way production builds
it and printing what came out — not by re-reading it. I had re-read all of it before merging.

**Rules:**

1. **Review the diff as its own step, after the gate is green.** "Tests pass" and "I have read
   this diff for defects" are different claims, and I have been treating the first as evidence for
   the second. It is not: every one of these four passed a full green `make check`.
2. **Execute the interesting path, do not read it.** For each new code path, construct the input
   the way production does and print the output. `playbook_note(...)` printed once would have shown
   the dangling link immediately; `with_id()` on a grown cluster would have shown two ids.
3. **When a module documents a failure mode, check my new code against it explicitly** — by name,
   as a step. Proximity to the warning is not protection from the bug.

## The obvious fix for a real gap can be worse than the gap (2026-07-31, v1 readiness PR 2)

Three items this session read as small and turned out to be mis-diagnosed — not by the person who
filed them, but by me when I planned the fix. In each case the plan said "populate X" and the
correct answer was "populating X naively makes something else worse".

- **`Note.confidence` has no machine producer.** The plan said: derive it from ELN record
  completeness. But `kg/conflicts._suspected` fires purely on a confidence *gap* between
  same-`(type, compound_smiles)` notes — so completeness-derived confidence would flag "one run is
  better documented than another" as a suspected conflict, manufacturing noise in a module whose
  own docstring says a wrong answer there is worse than none. The one principled source
  (calibration) reports `n=0` because nothing logs those predictions. I shipped the half that was
  real (`compound_smiles` on the reaction note) and rewrote the row with the corrected diagnosis.
- **`make eval` cannot fail on a regression.** The plan said: add `--strict` and use it in CI. That
  would have made CI permanently red, because two shipped cases exist to *demonstrate* a gate
  firing. The exit code was never the blocker; the missing concept was "expected to fail".
- **The retrieval eval scores one retriever.** The plan said: score them all. Scoring the derived
  paths needs the note index built over the eval fixture corpus, which needs Postgres — already a
  `DEFERRED.md` row. The fixable defect was narrower and worse: reporting a graph-only number under
  a name that promised the shipped path.

**Rules for myself.**

1. Before implementing "populate this empty field", read *every* consumer of it and ask what each
   would do with the value. A field with two consumers may want two different signals.
2. When a fix is "add a flag and turn it on in CI", run the flag against the shipped data first. If
   it fails, the flag was not the missing piece.
3. A row filed as [S] that turns out to need a schema change is not a small row. Say so in the
   backlog, with the size corrected, rather than shipping a smaller thing under the old label.
4. Rewriting a backlog row with what I learned is part of the work, not bookkeeping after it. The
   next session reads the row, not the diff.

## `git checkout --` does not revert an unstaged new file (2026-07-31)

My mutation-check helper ended with `git checkout -- "$file"` to undo the mutation. For a file that
was **new and untracked**, that fails with `pathspec did not match` — and because the helper had
already applied the mutation, the mutation stayed. Worse, for a *tracked* file the same helper
silently reverted my real edits along with the mutation: one run of it discarded the whole
`pr_gate.py` change I had just written, and I only noticed because a later `git diff` was empty.

**Rule.** Mutation-check by staging first (`git add -A`) and reverting with
`git checkout-index -f -- <file>`, which restores from the index rather than from HEAD. Then a
revert undoes exactly the mutation and nothing else, and it works for files that are not yet in a
commit.

**Rule.** After any scripted edit-and-revert loop, `git diff --stat HEAD` before moving on. A
silent revert of real work looks exactly like a clean tree.

## A test that a comment can satisfy is a test of the comment (2026-08-01)

Twice on one branch, and both times the *reasoning* I was proudest of is what broke the test.

- `test_every_worker_is_probed_and_scraped` asserted `"chemclaw.workerProbes" in text`. Deleting the
  connector worker's probes left it green, because the template's explanatory comment names the
  helper it is explaining.
- `test_two_replicas_may_not_be_one_node_or_one_eviction` asserted `"minAvailable:" not in budget`.
  It failed on a *correct* template, because the PDB's comment explains at length why
  `minAvailable` is the wrong choice there.

Both were caught — the first by a mutation, the second by the assertion failing on code I had just
written and knew to be right. Neither was caught by reading the test.

This repo's house style is long, argued comments in every template and module. That style and
substring assertions over source text are actively incompatible: the better the comment, the more
likely it contains the exact string the test is scanning for, in either direction. A false pass and
a false fail are the same bug.

**Rule.** Never assert a substring against a file this repo writes comments in. Assert the
*construct*: a template action anchored to its line
(`^\s*\{\{-\s*include "name"`), a YAML key anchored to its line
(`^\s*(minAvailable|maxUnavailable):`), a parsed value, or an executed route. If the assertion
cannot tell code from prose, it is not asserting about the code.

**Rule.** When a substring assertion is genuinely the only option, mutate *both* directions before
believing it: delete the thing and watch it fail, and add the string to a comment and watch it still
fail.

## `| head` under `pipefail` fails a step that succeeded (2026-08-01)

A CI step ran `syft <image> -o table | head -40` under `set -euo pipefail`. `head` closes the pipe
after 40 lines, the producer takes SIGPIPE, and pipefail propagates it: **exit 141 from a command
that did exactly what it was asked to do.** The step had already written a complete SBOM; the only
thing that failed was the human-readable preview of it.

The shape generalises past `head`: any short-circuiting consumer (`head`, `grep -m`, `sed q`) kills
a long-running producer, and `pipefail` reports that kill as the pipeline's result. It is
particularly nasty in CI, where the producer is often the expensive part and the consumer is a
convenience.

**Rule.** Never pipe a long-running producer into a short-circuiting consumer inside a `pipefail`
script. Write to a file and read the file — `cmd -o out.txt && head -40 out.txt`. It also removes
the second problem the same line had here: the "preview" re-ran a three-minute image scan to
produce a second copy of an answer already on disk.

**Rule.** When a CI step exits 141 (or 128+N generally), read it as a *signal*, not a failure of
the command's own logic, and look at what closed underneath it.

## An import inside a logging filter, and the tests my sandbox cannot run (2026-08-01)

`ContextFilter.filter` imported the ambient-identity getters lazily, which reads as the careful
choice: `core.*` must not depend on `agent.*` at module scope, so the import goes inside the
function. It shipped green through the full local suite and wedged CI.

A logging filter runs at moments its author does not choose. Temporal's workflow sandbox hooks
`__import__` and **logs a warning** when sandboxed code touches something restricted — so the
import tripped a restriction, the restriction logged, the log re-entered the filter, and the filter
imported again into a now half-initialised module. The workflow worker never recovered; the run
died on the global pytest timeout, pointing at an unrelated orchestrator test.

The second half is the one worth keeping. The offline sandbox **skips 98 tests** — 64 Postgres, 21
Temporal, 13 for missing binaries. "The full suite is green locally" therefore says nothing about
the workflow sandbox, the migration path, or the real DB constraints. I read a green summary line
that had `98 skipped` in it and treated it as coverage.

**Rule.** Code that runs on the logging path must not import, take a lock, or log. Resolve every
dependency at construction, at a known-safe moment (an entrypoint), and let `filter` do nothing but
read. The same rule covers `__repr__`, signal handlers and `atexit` hooks — anything callable from
inside an import.

**Rule.** Before calling a local run green, read the skip count and name what it excludes. If the
change touches a layer whose tests are all skipped offline (Temporal, Postgres, the container
build), say so in the PR body and expect CI to be the first real run — do not report it as verified.

## A test fixture is not the place to buy an assertion (2026-08-01)

The durable job record gained a measured `runtime_seconds`, and no test could tell a measurement
from a hardcoded `0.0`: the measurement happens inside a Temporal workflow, so the offline suite
cannot watch it and the server-backed test had nothing to bound it against. The fix I reached for
was to make the shared fixture child sleep a minute on the workflow clock — free under the
time-skipping server, in theory, and it broke the end-to-end test outright with
`No completion event found`.

Two things went wrong and only one of them is about Temporal. The Temporal one: time-skipping is
not a free knob you can turn inside a workflow that a test drives through several hops. The general
one is worse — **I changed a shared, expensive, hard-to-run fixture in order to strengthen one
assertion**, and the fixture serves four tests and only runs in CI. The cost of being wrong there is
paid in a five-minute CI round, which is exactly where I have least ability to iterate.

The claim was held instead over the **AST** of the call site: the `runtime_seconds` argument must be
a computed expression mentioning `workflow.now`, never a constant. It runs offline in milliseconds
and kills both mutations (hardcoded value; computed from the wrong clock).

**Rule.** When a claim lives somewhere no test can reach, look for a *different level* to assert it
at — the AST, the signature, the rendered config — before changing a shared fixture to create the
conditions. Modifying a fixture to make one assertion possible taxes every other test that fixture
serves, and taxes them in CI.

**Rule.** Parse, don't substring, when asserting about source. `"workflow.now" in source` is
satisfied by the comment above the line — the same trap already recorded twice above. `ast.parse` +
`ast.unparse` of the specific node is exact and costs three extra lines.
