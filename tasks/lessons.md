
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

## `git checkout -- <file>` reverts my own uncommitted work, not just the mutation

**Context.** Mutation-testing the "uncertainty reaches the note" change. I applied a mutation with
`sed`, ran the test, then "undid" it with `git checkout src/.../knowledge.py` — while the whole
feature was still uncommitted. The checkout restored the file from HEAD, deleting the mutation *and*
the ~40 lines of new code it was mutating. The next two mutation rounds then failed at collection
with an ImportError, which read like a broken test and was really a self-inflicted revert. I had to
re-write both files from context.

The tell was in the output and I nearly missed it: `Updated 0 paths from the index` on one round —
git reporting that there was nothing to restore, because the previous checkout had already flattened
it.

**Rule.** Commit before mutation testing. A mutation is a deliberate temporary edit on top of work
that must survive it, and `git checkout`/`git stash` cannot tell the two apart. With a commit in
place, `git checkout -- .` between rounds is exact and cheap; without one it is a delete.

**Rule.** Treat `git checkout`, `git restore` and `git stash` as destructive against uncommitted
work — the same class as `rm`. Before running one, ask what is in the working tree that is not in a
commit. "It only reverts the file I just edited" is true and irrelevant: I had also edited that file.

## An ADR that names a counter-example can license it

**Context.** Widening `prose-validate` to check ADR citations. Sub-decision labels like `D-A5a` are
real but live *inside* another ADR, so I derived the valid set by scanning the decision files. Then
I wrote the ADR for the change, and it said: "An invented `D-A77b` is still caught." The scan read
my ADR's body, found `D-A77b`, and licensed it — the test asserting it must fail started failing.

The rule was "a label some ADR mentions", and what I meant was "a label some ADR **defines**". Those
diverge the moment a document discusses the rule itself. Fixed by scanning only each ADR's title
line, which is where a defining ADR actually names its sub-decisions.

**Rule.** When deriving an allowlist by scanning documents, ask what makes an occurrence
*definitional* rather than incidental, and match that position — a heading, a declaration, a
specific field. "Appears anywhere in the corpus" is not a derivation, it is a wildcard, and the
first document to write *about* the rule will exploit it.

**Rule.** Writing the ADR is part of testing the change, not paperwork after it. This bug existed
for an hour and was invisible until the document describing it was added to the corpus it governs.

## Two checks over one corpus will collide on the fixture

**Context.** My new prose test needed a backticked path that does not resolve. `tests/
test_docstring_paths.py` already asserts that *every* backticked path in every source file resolves
— including test files — so the literal fixture failed a pre-existing gate. I changed the fake path
twice before understanding it was the fixture's *form*, not its value, that was wrong.

Fixed by building the string from parts (`"/".join((...))`) so no backticked path literal appears in
the source at all.

**Rule.** Before writing a test whose fixture is a deliberately-invalid instance of something the
repo validates, check whether an existing gate scans the test file itself. When one does, construct
the invalid value at runtime rather than writing it literally.

## A test can pin the shape of a control and never touch its effect

**Context.** The full-codebase review found 22 defects. Five of the highest-severity ones were
invisible to a green `make lint type test`, and in each case the project's *own guard* was what hid
them:

- `polynitro-aromatic`'s reference molecule was the one isomer its buggy SMARTS matched, so TNT and
  picric acid screened clean while the rule's test passed.
- The safety-gate fixture backticked structures the real writer does not emit, so a gate blind to
  every `bo-candidate` note had a passing test that appeared to cover it.
- `test_helm_chart.py` fed the note-repo path into `Settings` as modelled pod env — correct
  modelling — and never compared the result to the path the chart publishes to. It *encoded* the
  mismatch and asserted nothing about it.
- `test_config.py` pinned the permissiveness that let mock DFT energies key like real ones.
- The only entropy-against-experiment test used water, which is nonlinear, and so was structurally
  incapable of seeing a bug in the linear-rotor branch.

**Rule.** When a test exists for a control, ask what it would do if the control were *inert*. A test
that constructs the input the implementation happens to handle, or that asserts a value is wired
through without asserting what the value does, passes forever while the control does nothing. The
cheap check is a mutation: break the control, and if the test still passes it was pinning shape, not
effect.

**Rule.** For a rule table with one reference molecule per rule, a single molecule can demonstrate a
*motif* and can never demonstrate a *count* or a *position*. Any rule whose prose says "multiple",
"poly", "adjacent" or "on one ring" needs a negative case and an isomer, or the discipline is blind
by construction.

## Measure it; an argument between two plausible mechanisms settles nothing

**Context.** Three of four re-opened refutations changed conclusion once counted rather than
reasoned about. The sharpest: a review blamed a score sort for starving the lexical retrieval leg; a
verifier proved the sort was not causally responsible and stopped there. Both were half right and
neither had run it. Measured, the default mode delivered 38 graph chunks, 2 vector and **zero**
lexical — and removing the sort made it *worse* (40/0/0), so the sort was mitigating a different
cause neither had named.

The same pattern produced the DRFP finding: four separate places — a docstring, a second docstring,
an ADR and a closed backlog row — asserted a solvent-domination fix worked. Nobody had measured the
similarity. It was unchanged to the fourth decimal.

**Rule.** When two analyses disagree about *why* something is broken, stop arguing and run it. The
cost is usually one script; the alternative is picking the more articulate explanation, which is
uncorrelated with the true one.

**Rule.** A claim that a fix worked is worth nothing without the number. "Four places assert it is
delivered" is evidence about the authors' beliefs, not about the bits.

## Generalize the defect before fixing the symptom

**Context.** The review reported that `standardize()` turned NaOH into water, via a base-screen
symptom. Fixing exactly that would have left `NaBH4` → borane, `Pd(OAc)2` → acetic acid and
`n-BuLi` → butane in place. 18 of 87 shipped reagents were affected; the review named one.

Each was found by asking "what else does this mechanism reach?" rather than "is the reported case
fixed?" — and each needed a *different* discriminator (organic fragment, spectator counterion,
metal–carbon bond), asked at a different stage of the pipeline, because `Cleanup` destroys the
evidence the organometallic test needs.

**Rule.** A defect report is a sample, not a specification. Before fixing, enumerate the whole set
the broken mechanism touches — run the real corpus through it — and report the blast radius. The
reported case is rarely the worst one.

## Do not mark work complete because you analysed it

**Context.** I wrote detailed diagnoses of two findings, including the exact fix each needed, then
ticked both off. `git diff` showed neither file had been touched by any of the eleven commits. The
analysis read enough like a resolution that I stopped distinguishing them — and one of the two was a
defect I had introduced myself earlier in the same programme.

**Rule.** Before marking an item done, verify it against the artefact, not against your memory of
having thought about it: `git diff` the file, or run the test. Writing the fix down is not applying
it, and a convincing write-up is the easiest thing to mistake for finished work.


## A grading failure that defaults to a verdict is indistinguishable from a verdict

I wrote a live-probe harness, ran 190 questions through it, and reported that 46% of answers were
`unserved` and 36% `fabricated`. Both numbers were wrong, and the cause was in my harness.

The judge was capped at 1024 output tokens. A long reply truncated mid-JSON, `rfind("}")` returned
-1, and the parse-failure branch returned `verdict="unserved"` — **a real verdict value**. So 65 of
190 grading crashes were recorded as system failures, indistinguishable in `grades.json` from a
graded answer, and the bias ran toward hiding `fabricated` (five recovered prefixes showed the judge
had said `fabricated` before the cut). A fallback that reuses a legitimate value manufactures
findings and points them at the thing under test.

Two more in the same harness, both the same shape — a check that cannot see what it is checking:
the judge was passed tool *names* but never tool *results*, so it called verbatim quotations from
merged notes "fabricated" at rates of 40–67% depending on the slice; and the citation scorer
compared against a 200-character UI preview of a 20,000-character result, so grounded citations read
as invented.

And the one that stings most: I declared my own wikilink regex in the eval, stricter than the
production one in `kg.note`. An answer whose nine `[[**id**]]` citations were *every one of them
dangling* scored a clean citation record, because "cites nothing" and "every citation grounded" were
the same result. Two readers for one syntax is how a gate comes to disagree with the thing it gates.

**Rules.**
1. A verdict that cannot be obtained must be its own value (`ungraded`), never a member of the
   normal range. If a parse failure can be spelled the same way as a result, it will be.
2. Before quoting a number a grader produced, check what fraction of its outputs it actually
   parsed. I reported the distribution before checking, and had to retract it.
3. A checker must be given the evidence the thing it checks was entitled to use. A judge shown
   only tool names is guessing, and it guesses "invented".
4. Never redeclare a pattern the production code already owns — import it. A stricter local copy
   fails open and looks clean.
5. When a subagent's analysis contradicts your headline, it is more likely right than the headline:
   it read the artefacts and you read your own summary. Three of four corrections here came that way.

## 2026-08-02 — Six teams, one tree: what the parallel implementation pass taught

**A code audit is evidence about what the author read, not about what runs.** The roadmap said
threading `n_generators` through `factorial_design` was a one-line change — the parameter exists on
the imported BoFire class, the docstring even explains it. Team B ran it: 128 runs at
`n_generators=0`, 128 at 1, 128 at 2. BoFire fractionates only the *continuous* half of a domain and
crosses the categorical half in full, and `factorial_design` is all-categorical by its own refusal.
The parameter was inert on the only domain shape that reaches it. This is the second time in one
session that a measurement beat a documented claim — the first was the solvent-domination fix that
two docstrings, an ADR and a closed backlog row asserted, and that changed the similarity by zero to
the fourth decimal.

**Rule: a plan item that says "just thread X through" gets X measured before an agent is told to
thread it.** One script, thirty seconds; the alternative is an agent implementing a no-op elegantly.

**`git stash push src/` is a shared-tree hazard, and it is the *converse* of the earlier one.**
Earlier this session I stashed everything to prove a test failed without its fix, which took the
test with the source and proved nothing. The brief for the six teams therefore said "stash only
`src/`". With six agents in one working tree, that reverts *every* team's source, not the stasher's
— three teams hit it independently. The two that got it right did per-file `git checkout --` on
their own paths, or a detached worktree.

**Rule: "revert the source to prove the test fails" needs a scope that matches the writer, not the
repo.** Per-file checkout when the tree is shared; a worktree when the change is broad.

**A one-caller helper written to dodge a cross-team dependency must be collapsed at integration.**
Team E could not add the config field it needed (another team owned `core/config.py`), so it wrote
`_shape_gate_enabled()` and left a comment saying to point it at `settings` once the field existed.
That comment is the reason it got collapsed instead of surviving as permanent scaffolding.

**Rule: when an agent reports "I needed something outside my paths and worked around it", the
workaround is an integration TODO with a name, not a delivered design.**

**Ownership by file, not by feature, is what made six concurrent agents work at all.** Several items
from different waves land in the same module. Cutting teams by wave would have put two agents in
`api/runner.py`; cutting by file put all three answer-path items in one serialized agent and left
the other five genuinely disjoint. The waves survived as the *PR* boundary, not the work boundary —
and where a team's items straddled two waves (BO: one wake-up, one new capability), the PR boundary
bent rather than splitting a file across commits.

## 2026-08-02 — The adversarial pass, and the two things it caught that I had signed off

The reviewer owned no code and read the diff. It found two merge-blockers in work I had already
run the full gate on, and both were the same species: **a change whose test was edited to fit it.**

**`stoichiometry_table` rejected correct calls.** The reasoning was "a substance with a density is
a solvent, so charging it by molar equivalent is the error we just fixed". Ten entries in the
density table are routinely charged by equivalent as reagents — AcOH at 1.5 eq, water in a
hydrolysis, MeOH in an esterification, DMSO as the Swern oxidant, DMF as the Vilsmeier reagent —
and because `density_of` resolves SMILES, there was no spelling left that could charge water by
moles at all. **The existing test that would have caught it was rewritten to the new signature**,
silently turning 1.0 equivalent of water (18.0 g) into 2.552 equivalents (45.91 g). It asserted
only thread identity, so it passed. My ADR then asserted "there is no reading under which it was
right", which the reviewer refuted in four lines of output.

**Rule: when a change requires editing an existing test, that edit is the finding.** Diff the
*values* the old test asserted against the new ones and say out loud why they differ. "Updated the
call to the new signature" is a sentence that hides a behaviour change every time.

**Rule: before enforcing a predicate, check the predicate is the one you mean.** "Has a density"
and "is charged by volume" differ by one being a fact about a substance and the other a fact about
an experiment. A rejection is a much stronger claim than a conversion and needs the stronger
evidence.

**The substring hole.** `turn_evidence` grounded a citation with `note_id in output`. Note ids are
not prefix-free, and the *committed corpus* carries `playbook-degassing` and
`playbook-degassing-old` — so a turn that retrieved only the retired note certified a citation to
the current one at confidence 1.0. That is the exact failure the function exists to catch, in the
commit that added it.

**Rule: an identifier match is a token match. Never `in`.** And when a check is about ids, go
looking in the real corpus for a prefix pair before claiming the check works.

**And the near-miss in my own verification.** Proving the fixes, I made a git worktree at the
pre-fix commit, copied the new tests in, ran them — and all seven **passed**. Not because the tests
were wrong: pytest resolved `chemclaw` through the editable install pointing at the *main* tree, so
I was running new tests against new source in a directory that looked like old source. `PYTHONPATH`
set to the worktree's `src/` flipped six of seven to failing, which is the real result.

**Rule: when proving a test fails without its fix, print the module's `__file__` first.** A
worktree, a container, a `sys.path` entry and an editable install all give you a directory that
lies about which code is running. The check costs one line and is the difference between evidence
and theatre — this is the third variant of the same trap in one session.

## 2026-08-02 — Reviewing my own fix commit, and the shape a "shared" data structure hides

The adversarial pass reviewed waves 1–3. It did **not** review `7033c1c`, the commit that fixed
its own findings, and that is where the one real defect was.

**Rule: the fix commit for a review needs its own review.** It is written fastest, under the most
confidence, and by the person who just proved they missed something.

**The defect: a data structure shaped for one consumer, rendered verbatim by another.**
`turn_evidence` emits one `EvidenceChunk` per *(tool output x cited id)* pair, each carrying the
full output text. That is right for `verify_claims`, which reads only `{chunk.source_note_id}` and
never touches `content`. It is quadratic for `_verifier_prompt`, which renders every chunk — one
~20,000-character `gather_evidence` result cited by 40 ids became a **749,531-character** judge
prompt, 40.1x. Grouping by content in the prompt builder alone takes it to 1.1x.

Two things made it invisible. The duplication is *free* on the default path (`verifier_enabled`
defaults False), so no test and no run could feel it. And it scales with the behaviour the system
is trying to encourage — an answer that cites its sources well is the answer that blows up.

**Rule: when one producer feeds two consumers, check what each actually reads.** "The list has N
entries" and "each entry carries 20 KB of text" are different facts, and a consumer that only
needs the keys will not tell you the values are being copied.

**Rule: measure a size before shipping a structure that carries text.** One loop over
1/5/10/40 citations turned an opinion into a table, and the table is what made the fix obviously
worth doing rather than arguably worth doing.

**A smaller one, twice now: a correction has to be applied everywhere the claim appears.** The
"the eval harness scores this way too" sentence was refuted once and lived in three places; I
fixed the module docstring and the ADR and left the function docstring asserting it twelve lines
below the disclaimer. `grep` for the *claim*, not for the file you remember writing it in.

## 2026-08-02 — Fanning six worktree agents out twice: what the R0/R1 waves taught

**A subagent's own gate claim is not evidence; the integration run is.** One R1 package reported
"`make lint type` both green" and arrived with five lines over the limit. Another reported the same
and was clean. The difference is invisible from the report, so the only safe reading is that a
worktree gate constrains nothing about the merged tree. **Rule:** never let a WP close on its
author's gate claim — run `make lint type test` over the *combined* result before the PR, and
expect it to find something.

**A worktree does not inherit your uncommitted — or unpushed — work.** I fixed the
`test_repo_map` dot-path bug first, deliberately, so the six agents would run against a clean
suite. All six worktrees were based on the pre-fix tree anyway: every agent hit the bug, two spent
real effort proving it was environmental, and one independently fixed it, which was the only merge
conflict of the wave. **Rule:** commit *and push* a prerequisite before launching worktree agents,
then verify with `git merge-base --is-ancestor <fix> <agent-branch>` rather than assuming.

**The bugs that only exist between the packages are the ones nobody is assigned to find.** Three
defects in these two waves were invisible to every individual agent and appeared only when the
work was combined: a docstring's example URL (`https://attacker/`) tripping an unrelated egress
guard that scans all host literals in `src/`; a new `safety-validate` target wired into CI by one
package while the `make ci` meta-target added by another did not list it — the exact drift that
target exists to prevent; and a config change refusing `service_uvicorn_workers>1` while the
entrypoint still passed `--workers` and the chart still advertised it. **Rule:** at integration,
diff what each WP *declared* against what the other WPs *changed underneath it*, especially for
build files, CI wiring and any list one package extends and another consumes.

**Invite the pushback explicitly and it will arrive on the one that matters.** Two prompts said, in
effect, "if this is wrong when you read the code, say so rather than forcing it". Both were taken:
the connector-redirect fix I had specified as "A **or** B" turned out to require both (httpx copies
the original headers forward *and* the hook re-adds them, so declining to re-stamp closes nothing),
and reparenting `AuthorizationError` under `ChemclawError` — which I asked for — would have made
`surface_domain_errors` catch every authorization refusal before `surface_authorization_denials`
could, silently turning "Refused: …" into "Error: …". Registering the name without reparenting was
the right fix, and the agent traced the middleware ordering to prove it. **Rule:** a plan's proposed
remedy is a hypothesis; write the prompt so disproving it is a success condition, not a deviation.

## 2026-08-03 — Closing the refactor: what the orchestration itself cost

**A worktree agent's base commit is whatever the harness had lying around, not your branch tip.**
One Wave-1 agent was silently cut from a base missing two whole phases of merged work and built on
it without noticing — nothing in its own view looked wrong, because a stale tree is internally
consistent. The fix that held afterwards was a mandatory first step in every agent brief: fetch,
compare `HEAD` against the named tip, rebase if behind, and confirm by checking for concrete markers
(a file, a symbol) that the phases you depend on are present. **Rule:** never let a worktree agent
start work before it has *proven* its base, by marker, not by `git log` looking plausible — and put
that proof in the brief as step zero, because an agent that skips it reports success from the wrong
universe.

**Never run the test gate while other processes load the box.** The same suite, same commit, same
day: 312 s and green on a quiet machine; 1330 s (4.25×) and two failures with parallel agents
building beside it. The two failures were the two slowest tests in `test_pka.py` (11.8 s / 11.2 s
alone) — the tests with the least headroom are the ones a slowdown converts into false findings,
and they pass 27/27 in isolation. **Rule:** the gate run that decides anything runs alone; a red
test observed under load is an *observation about load* until reproduced quiet. (The failure text
from the loaded run was lost to a `| tail` — capture to a file first, the same lesson `pipefail`
taught from the other side.)

**All three Wave-1 agents independently parked themselves waiting on a background `make test` and
had to be resumed by hand to commit.** Same shape three times in one wave: work finished, gate
started in the background, agent idle until poked — the commit, the one artifact the orchestrator
needed, held hostage to a verification that could have followed it. **Rule:** brief agents to
commit first, then verify — a commit is cheap to amend or revert if the gate fails, but an
uncommitted finished change is invisible to the orchestrator and blocks the whole wave on a timer
nobody is watching. More generally: any long-running verification goes *after* the state that
matters is durable.

**"Zero test files changed" and "no assertion changed" are different claims — verify the one that
was made.** R5.3's re-verification found the runner split (3dbb009) touched four test files, all
mechanical re-points of import paths for helpers whose names went public — while its own commit
message claimed exactly that ("import re-points only, no assertion changed") and was accurate. The
false stronger claim ("zero test changes") existed only in a later retelling. **Rule:** when
relaying a verification claim, quote the artifact's own wording rather than strengthening it;
"behavior-preserving" hardens into "byte-identical" over two retellings and then fails an audit
that the original claim would have passed.

## 2026-08-04 — A wait loop that cannot terminate is a defect that reports itself as a test failure

**Three "failures" in the W2 gate were all my own busy-loop.** The merged-W2 run took 30:37 against
a normal 4:34 and produced three timeout failures; `test_pka` and `test_reizman` then reproduced
timing out *alone*, which is exactly the signature of a real defect. They were not. One of my own
wait commands was `until [ "$(date -u +%s)" -ge "$(( $(date -u +%s) + 1 ))" ]; do :; done` — a
condition comparing *now* against *now + 1*, which can never be true. It had been spinning a full
core of a 4-core box for 43 minutes. Killed it; both tests passed together in 69 s, and the full
suite in 4:34.

This is the exact shape the R5.3 lesson above already names — "a red test observed under load is an
*observation about load* until reproduced quiet" — and I re-ran the tests quiet and still got red,
which felt like the reproduction that rule asks for. It was not, because the load was **mine and
invisible**: no `pytest`, no `make`, nothing in the task list, just a shell. `uptime` showed load
5.30 on 4 cores against a `ps` whose top consumer was 17% `bash`, and that mismatch was the only
evidence available.

**Rule: before believing a timeout, read `uptime` — and if load is high while `ps` shows no obvious
consumer, the consumer is a shell loop of your own.** A busy-wait does not look like work.

**Rule: `until ! pgrep -f pytest; do sleep 20; done` never exits.** The waiter's own command line
contains "pytest", so it matches itself and waits forever. Match on something the waiter cannot
contain (a marker file, a completion sentinel) or use the harness's own completion notification,
which is what it is for.

**Rule: a wait condition gets read twice before it is run.** `>= now + 1` and `> now` differ by a
character and by whether the machine stays usable.

## R5.5 — A measurement script is code, and an unrun one is a claim

**What happened.** The BO roadmap's measurement register recorded "10 rows, 5 folds → R² 0.948,
MAE 1.47" for `cross_validate`, and that number travelled into an ADR, a maintained capability map
and a BACKLOG row. Re-running the register at the end of the roadmap, the script that produced it
**raises**: it passes `get_metric` the string `"R2"`, and BoFire builds the result Series with
`name=metric.name`, which a `str` does not have. The pair cannot be produced by anything in the
scratchpad. The *finding* it supported is solid — `cross_validate` runs off
`strategy.surrogate_specs` with no class named — and reproduces at 0.935, 0.950 and 0.813 across
three routes. Only the number was fiction, and it was the most quotable part.

The whole point of the register was that "prose is evidence about what its author believed, never
about what the code does". A number transcribed from a script whose failure branch printed a message
is prose wearing a number's clothes.

**Rule: a measured number is only measured if the script that produced it runs green today.** Keep
the scripts, re-run them at the end of the work they justified, and treat a `try/except` that prints
a diagnostic as a place where a number can quietly stop existing.

**Rule: when a measurement is retracted, retract it where it was cited** — the maintained document
and the open backlog row, not only the new ADR. A merged ADR is never edited, so the correction has
to be findable from the places a reader would actually reach for the number.

## R5.6 — A green suite proves the code does what the test says, not that the test is right

**What happened.** W1's `campaign_progress` reported a campaign that climbed **50.0 → 70.9 against a
stated ±2 assay noise** — ten times the noise — as `plateaued`. The counter compared each run to a
continuously updated running best, so a climb in sub-noise steps never reset it. The tool exists to
stop a lab leader being misled about noise, and it misled in the opposite direction: stop a campaign
that is working.

It shipped green. There was a test asserting exactly this behaviour, and its docstring argued for
it — *"what makes a gain real is the assay, not the slope"* — with a deliberately-built pair of
tests around the claim. The sentence is true of one step and false of a series, and nothing in the
suite could tell the difference, because I had written both the code and the argument for it.

R5.5 says a measured number is only measured if its script runs green today. This is the same defect
one level up: **a test is evidence about what its author believed the code should do.** A green
suite says the code matches the belief. It says nothing about the belief.

**Rule: for any threshold, ask what happens just under it, repeatedly.** One sub-threshold step is
inside the noise; twelve of them are not. Single-step comparisons hide accumulation, and every
"is this difference real" test has a series version that behaves differently.

**Rule: when a test docstring argues for its assertion, that is where to look hardest.** A test that
merely records behaviour is cheap to re-derive. A test that comes with a paragraph defending a
counter-intuitive result is the one carrying a decision, and a decision is the thing that can be
wrong. Both defects this review found were under such a paragraph.

**Rule: for a tool that advises stopping or continuing, write down which error is worse.** Here they
are not symmetric — a false "keep going" costs a fortnight, a false "you have plateaued" costs the
rest of the campaign and is invisible afterwards, because nobody measures what a stopped campaign
would have found. Naming that asymmetry up front would have made the defect obvious at design time.

## R5.7 — `git add -A` is a claim about a tree you no longer control

**What happened.** Three review subagents were reading and *mutating* the working tree — the whole
point of the technique is to delete a guard, run its tests and see whether they notice. One of them
did not restore what it deleted. Fifteen minutes later I committed the soak work with `git add -A`,
and the commit removed two lines of production code from `run_turn`: the durable-subsystem
reachability probe that announces a dead Temporal broker before the first token.

Nothing about the soak touches the front door, so the diff was visibly wrong and I did not look at
it. `git status --short` had shown `M src/chemclaw/api/runner.py` in an earlier turn and I read it
as an agent's scratch state rather than as something my next commit would adopt. The four tests
that pin the announcement fail on the deleted version, so CI would have caught it — which is a
reason it was cheap, not a reason it was acceptable.

**Rule: never `git add -A` while an agent that can write is running.** Stage the paths the commit is
about, by name. The convenience is worth one file; it is not worth adopting another process's
uncommitted state.

**Rule: read `git diff --cached --stat` before every commit and ask whether each file belongs to the
message.** A file the commit message never mentions is the signal — here, `api/runner.py` in a
commit about a soak script.

**Rule: an agent told it may mutate the tree must be told how to prove it restored it**, and the
proof is `git diff --exit-code <paths>` at the end, not a sentence saying it cleaned up. Two of the
three said the tree was clean; one of those two was reporting on files it had not touched.
## R5.8 — `-p no:randomly` was a no-op, and a loop over an empty list asserts nothing

**Two failures of the same kind, both found by a review rather than by the suite.**

**The flag that did nothing.** Every test command I ran this session carried `-p no:randomly`, and I
reported results as though test-order randomization were controlled. `pytest-randomly` is **not
installed** — it is in neither the venv nor `uv.lock`. `pytest -p no:X` silently accepts a plugin
that does not exist, so the flag disabled nothing and proved nothing. I had been asserting a
property of the run that I had never checked.

**Rule: a flag is not a control until you have seen it take effect.** Check the plugin is installed
(`pytest --trace-config`, or look in the lock file) before describing a run as controlled for what
that plugin does. A silently-accepted no-op is indistinguishable from a working guard.

**The loop that could not fail.** Five constraint tests were written as
`for candidate in <strategy call>: assert <property>`. A strategy returning `[]` passes all of them.
This was not hypothetical — BoFire was already short-changing one ask (`Expected 3 candidates, got
2`), so the wave's headline claim, that the seeding path honours a stated limit, was never actually
pinned. The fix is one line per test: bind the result, assert its length, *then* loop.

**Rule: a `for` loop is not an assertion.** Any test shaped `for x in f(): assert p(x)` also needs
`assert len(...) == n`. The same applies to `all(...)`, `sum(...) == 0` and every other reduction
over a collection the code under test produced — they are all vacuously true on empty.

**What both have in common:** each looked like a check and was a description. R5.5 said a measured
number is only measured if its script runs green; R5.6 said a green suite proves the code matches
the test, not that the test is right. This is the third face of it — **a guard that never executed
is not a guard**, whether it is a plugin flag, a loop body, or an assertion the data never reaches.

## 2026-08-05 — Database integration review

**"Apply exactly once" is the wrong semantics for a reconciliation.** The plan put the privilege
grants in `infra/sql/` as migration `036`. That set is applied once per file and tracked by
checksum, which is right for a schema change and silently wrong for a grant: a deployment creating
its runtime role *after* the first `db-migrate` would never have grants applied at all, and every
table added by a later migration would ship ungranted and break the application on first use of it.
Neither failure is visible at deploy time — the ledger reports success both times. Caught only by
applying it to a live database, creating the role, and noticing the second run did nothing.

**Rule: before putting something in a run-once mechanism, ask what happens when its *inputs* change
after it has run.** Schema DDL has no such inputs; a grant depends on a role and on a table set that
both keep moving. Migration-shaped and reconciliation-shaped work look identical in a directory
listing and are opposite in their cadence.

**A derived check with a blind spot is worse than no derived check.** `test_database_privileges.py`
derives the grant matrix from the SQL literals in `src/` — and its first version walked `ast.Constant`
only, so a statement containing one f-string interpolation arrived as *fragments*: `INSERT INTO x`
separated from its own `ON CONFLICT ... DO UPDATE`. The upsert's UPDATE therefore vanished, and the
test confidently reported two correct grants as over-grants. It failed in the safe direction by
luck; the same gap in the other direction would have removed a privilege the application needs and
called it tightening. A second version fell to the same class — the grant *parser* read the file
line-wise while Postgres reads adjacent string literals concatenated, so it saw only the
single-line grants and reported every other table as ungranted.

**Rule: when a test derives a fact from source, first check it can see the shapes that source
actually uses.** Print the derivation's raw output and read it against the code before trusting
either direction of the assertion. Both failures above were found by looking at the list, not by
the test going red — a derivation that under-reports produces *green* on the missing half.

**What this shares with R5.5/R5.6 and the vacuous-loop entry above:** each was a mechanism that ran,
reported, and did not check what it claimed. The new face here is that a *derivation* has two
failure directions, and only one of them is loud.
## 2026-08-05 — `git checkout <file>` is not an undo when the file has uncommitted work

**The revert that destroyed the work it was reverting.** To prove a new drift-guard test was not
vacuous, I mutated `chemclaw_agent.py`, ran the test (it failed — the guard was real), then ran
`git checkout src/chemclaw/agent/chemclaw_agent.py` to undo the mutation. That restored the file to
**HEAD**, not to its pre-mutation state, deleting an hour of uncommitted edits to the same file. The
mutation check succeeded and cost more than it proved.

**Rule: to mutation-check a file with uncommitted changes, copy it aside first**
(`cp f /tmp/f.bak` … `cp /tmp/f.bak f`), or commit before mutating. `git checkout`/`git restore`
take their content from the index or HEAD and have no notion of "the state I was just in" — there
is nothing to recover from afterwards, because the working copy was the only copy.

**The general shape:** an undo is only an undo if it restores the state you actually left. Every
`git` command that writes the working tree (`checkout`, `restore`, `stash`, `reset --hard`) resolves
against a *committed* baseline, so on a dirty file each of them is a delete dressed as a revert.

## Measure the hot spot before refactoring it (2026-08-05, agentic-engine review)

Three things looked like obvious waste on the turn hot path. I wrote a benchmark for all three
before touching any of them, and **two were noise**: the harness todo re-read per streamed update
is 14 µs (21 ms across a whole turn), and `GraphRetriever` building 2,000 evidence chunks for the 40
`gather_evidence` keeps costs 5.8 ms against a 3 ms scan that has to happen anyway. Bounding the
per-source list would have changed how `hybrid` mode fuses ranks — a real semantic risk — to buy
nothing measurable. The third was 2,458 ms per sweep.

**Rule:** "this runs per token / builds N and keeps 40" is a *hypothesis about* a cost, never the
cost. Benchmark all the candidates in one script before editing any of them, and let the numbers
pick which one to fix. The two changes I did not make are the review's best outcome, not its
leftovers.

Corollary, from the same benchmark: the first number was misleading too. `_conflict_index` measured
892 ms on a corpus I built with 7 substrates, and 11 ms on the same 2,000 notes spread over 2,000
substrates. The cost lived entirely in the corpus *shape*, so I re-ran across three shapes before
quoting anything — and the shape that is slow (many runs on one substrate) turned out to be the
shape this system exists for, which is the argument the finding needed.

## A rule written in three places is three rules (2026-08-05)

`X if profile.X is None else profile.X` for the harness dimensions appeared in `build_agent`, in
`_resolved_autonomy` and in `gate_applies` — and the repo's own history records what that already
cost: a fourth site (`api/runner.py`) read `settings` directly, so a `plan_only` profile under a
global `execute` never spent its approval. Two `PlanEvent` emit sites had drifted the same way, one
guarding on truthiness and one on `is not None`.

**Rule:** when a review finds the same conditional in two places, do not fix the divergence — remove
the ability to diverge. A helper that holds the state *and* the predicate (`_PlanEmitter`) makes two
call sites identical by construction; two call sites that both remember to call the same predicate
are still two rules. And check the docstrings while you are there: `_build_harness_agent` claimed its
instructions were "pre-resolved by `build_agent`" while resolving them itself — the prose described
the code that should have existed, which is exactly the drift CLAUDE.md's "measure it, don't argue
it" is about.

## R5.9 — A ceiling is a claim about a limit, and claims get measured

`bo_max_rounds=500` was documented, in two places, as the thing keeping a durable campaign inside
Temporal's event-history limit. It was not. The history is re-sent to the propose activity every
round, so bytes grow quadratically, and at a measured 178 bytes per `Observation` a batch-1
campaign crosses the 50 MB hard limit at round **441** — inside the ceiling. A campaign at the
documented maximum would have been terminated by the server with every paid evaluation lost, by
exactly the failure the ceiling was written to prevent.

Nobody had multiplied. The number was chosen as "generous versus the default of 10", the *reason*
was written beside it, and the two were never checked against each other. The reason read as
derived because it was specific and mechanistically correct — the history really does grow
quadratically — and being right about the mechanism is what made it convincing while being wrong
about the number.

**Rule: a config bound that names a system limit must show the arithmetic that reaches it.** If a
comment says "this keeps us under X", the value's derivation from X belongs in the comment, or the
bound is not doing what it says. And when the arithmetic does not close — as here — the fix is
usually not a smaller number: a number can only be right for one problem shape, so prefer the
signal the platform already publishes (`is_continue_as_new_suggested()`) over any constant a
reviewer would have to re-derive.

**Corollary, from the same review:** the ceiling was documented in the config comment, in
`require_rounds_within_ceiling`'s docstring, *and* in a test's docstring — three copies of one
wrong claim, all agreeing, none measured. Agreement across copies is not corroboration; they are
one statement, written once and pasted.

## R5.10 — "It skips here" is not a reason to migrate its call sites blind

Changing `upsert_campaign` + `add_suggestion` into one atomic `record()` meant rewriting every call
site in `tests/test_postgres_campaign_store.py` — a file whose every test **skips offline**, because
the sandbox has no Postgres. I rewrote them mechanically, saw 3242 local tests pass, and pushed. CI
failed three of them.

The mechanical substitution was wrong in a way only running it shows: several tests opened with
`await store.upsert_campaign(...)` as *setup* — create the campaign row, no suggestion. Replacing
that with `record(campaign, Suggestion(...))` inserted an extra suggestion row each time, so a test
asserting two suggestions found three, and a test unpacking `[suggestion] = suggestions_for(...)`
got two and raised `too many values to unpack`. The old API had two verbs because the two writes
were separable; collapsing them collapsed a distinction the tests were using.

**What I should have done, and did second:** get the dependency running. `postgresql-16` was already
installed in this sandbox — `initdb` + `pg_ctl` as the `postgres` user, `apt-get install
postgresql-16-pgvector`, and a real database existed in about two minutes. The full migration still
would not apply (pgvector 0.6.0 has no `bit_jaccard_ops`; 002/003 need >= 0.7), but
`031_bo_campaigns.sql` depends on nothing else, so applying that one file alone and driving the
store from a script measured all three failures — and two claims I had written but never run: that a
non-finite float is refused, and that the rolled-back transaction leaves no campaign row.

**Rule: before editing tests that skip in this environment, spend five minutes trying to unskip
them.** A skipped test is not a passing test, and a green local suite that skipped the file you just
rewrote is evidence about the other files. Where the dependency genuinely cannot run, say so in the
PR *and* reproduce the changed test bodies in a script against whatever partial substrate does run —
"CI will tell me" is a slower, more public version of running it.

**The narrower trap:** when an API change merges two calls into one, the call sites that used only
*half* of the old pair are the ones that break. Grep for the callers of each old name separately
before replacing either.

## R5.11 — A refactor's first job is to find which of two copies is right

Every finding in the 2026-08-05 knowledge-management review had the same shape: a rule written in
two places, with a docstring in one of them asserting the other agreed. `note_text` said "one
definition … cannot drift" while three haystacks drifted. `_matches` said "matching mirrors
`find_notes`" while building its own. `kg/proposal.py` said a failed row is replayable "because the
bytes it would have written are still here" while storing `files[0]`. `git_submitter.py` said the
checkout returns to base "on every exit" and then enumerated exceptions.

The habit that turns this from a reading exercise into a measurement is small: **when a comment
claims two things agree, compute the difference.** Not "do they look equivalent" — set-difference
them over the real corpus. It took nine lines to learn that five notes and fourteen notes were
each visible to one reader and invisible to another, and no amount of reading the three functions
side by side would have produced those numbers.

The second half is that a *union* is a behaviour change and needs its own evidence. Widening the
retriever's haystack was justified by re-running the gold set before and after, not by the
argument that the union is obviously better. The rule was "one definition", not "this one" — the
measurement chose which.

Rules:
1. When prose asserts two implementations agree, diff them over real data before believing or
   disbelieving it. The script is usually under twenty lines.
2. When consolidating N implementations into one, the choice between them is a behaviour change:
   measure the gated number both ways and record both, even when you expect no movement.
3. When a docstring enumerates exceptions to its own claim ("on every exit — except…"), the claim
   is already false. Treat the enumeration as the finding.

## R5.12 — Inverting a test is not the same as rewriting it, and the difference is which one still tests

`tests/test_pr_gate_read_window.py` was written to pin a measured defect so its fix would have a
regression target, and it said so. Three of its four tests drove `git checkout -B` by hand — so
against the fixed submitter they went green, unchanged, having tested git rather than ChemClaw.
The file that existed to prevent "an assertion true under every implementation" contained three.

Sign-flipping them would have preserved that. The fix was to make each test drive the real
submitter, and to pair every absence assertion with a positive one — "the note is not in the
shared tree" is also true of a submission that never ran, and the non-vacuity check
(`ls-tree` on the branch really does contain it) is what separates the two.

Then: every new test was run against a mutation that removes the behaviour it claims to pin. Four
mutations, four expected failures. That is cheap — one `sed`, one `pytest` — and it is the only
thing that distinguishes a regression target from a description of one.

Rules:
1. Before inverting a test that pinned old behaviour, check what it actually drives. If it drives
   the standard library or a subprocess rather than the code under test, it needs rewriting, and
   inverting it would hide that.
2. Pair every "X is absent" assertion with evidence that the run which should have produced X
   happened at all.
3. A test written as a future regression target earns its name only after you have watched it fail
   for the right reason. Mutate, run, restore — three commands.

## R5.13 — A backlog row saying "this needs a decision" is a claim about the code, and claims get checked

Two rows in this pass were filed as decisions rather than diffs, and both were wrong about the code
they described.

The BO row said recording a durable campaign meant "either threading identity through a seam built
to keep it out, or writing a fabricated actor into an audited column". The seam was built to keep
identity out of the **payload** — a memo is per-execution metadata beside the argument, the
distinction is stated in `connector_job.py`'s own comment, another bundle has read that memo in
production since F5, and a test already pinned the crossing. Three pieces of evidence, all present,
none consulted when the row was written.

The conflicts row said a per-note cap "changes what KM-8 shows a chemist, which is why it is a
decision and not a patch". `Conflict.kind` already separated author-stated from heuristic, so the
cap applies to `suspected` alone and the declared-conflict promise is byte-identical; and the gap
magnitude was already computed at the line that decides whether to report a pair at all, so
"widest first" was `max` instead of `append`.

Both rows were written by careful sessions in the middle of good reviews. The failure mode is
specific: a reviewer reasons about a seam from its *purpose* ("core owns attribution") rather than
from its *mechanism*, reaches a genuine dilemma, and files it — and the dilemma is real only under
the reasoned-about version.

Rules:
1. Before accepting an inherited "this is blocked on a decision", spend ten minutes finding the
   mechanism it names and reading it. A blocker is a claim about code, and it decays like any other.
2. When a row says a fix is impossible without one of two bad options, look for the third: the
   thing whose docstring already describes the case. If a comment in the codebase states the exact
   situation, that is not a coincidence — someone built for it.
3. Grep for a production reader before concluding a mechanism cannot be used. `connectors/qm`
   answered the BO question in one line.

## A fix that passes every test written for it (2026-08-06, whole-codebase security sweep)

Twice in one session a change passed every test I wrote *for that change* and broke something else,
and both times the full suite was the only thing that said so.

**`configure_logging()` in `connector_app`.** Connector servers had no secret redaction, and
`connector_app` is the single point all seven bundles pass through — visibly the place a new bundle
could not forget. It is `logging.basicConfig(force=True)`, which removes *every* root handler, and
that function runs at import time in modules tests and the dev composite import freely. It tore out
pytest's own capture handler and failed two GxP audit-trail tests that have nothing to do with
logging. The targeted tests all passed.

**"A shipped default is not a credential."** The dev Postgres password is the literal string
`chemclaw`, so redaction was replacing the product's own name with `***`. I compared whole values
against the field defaults. `tests/conftest.py` repoints `postgres_dsn` at an isolated schema, so
under CI the DSN is *not* the default — redacted correctly — while the password inside it still is.
Locally there is no Postgres, nothing repoints, and it passed. CI failed.

The shape is the same both times: the change was correct about the thing it was aimed at, and wrong
about a *context* the targeted test could not contain — an import-time side effect, and an
environment where a fixture rewrites the input.

Rules:
1. **A test written alongside a fix shares the fix's blind spot.** It is evidence the mechanism
   works, never evidence nothing else broke. Run the full suite before believing a fix, and read
   which tests moved rather than only the exit code.
2. **Ask what the function does to the *process*, not only to its arguments.** `basicConfig`,
   `set_meter_provider`, `contextvars`, module-level caches and monkeypatched singletons are all
   process-wide. A process-wide side effect belongs at a process boundary, never in a composition
   helper that anything may import.
3. **When a rule compares a value against a default, ask what rewrites that value in each
   environment.** A conftest fixture, a chart, an env var and a local `.env` are four different
   inputs to the same comparison, and the sandbox exercises the fewest of them. This is the
   concrete meaning of "CI is the arbiter, a local green is not the gate".
4. Both wrong turns produced a *better* final fix than the one I would have shipped — the connector
   entrypoint that did not exist, and a rule covering derived values. Record the wrong turn in the
   ADR: "the single composition point" is a trap the next reader will find equally attractive.

## Two probes that measured nothing, confidently (2026-08-06)

Sizing an O(n²) blowup in the safety screens took three attempts.

1. **Repeated one SMILES 400 times.** `screen_reaction` starts with `dict.fromkeys(...)`, so it
   deduplicated to two molecules. Flat timings, no growth: a clean "no defect here" result.
2. **Generated distinct molecules by growing a chain with the index.** Beautiful quadratic curve —
   which was the total *atom count* of my own input, since substructure matching costs scale with
   molecule size. It would have "confirmed" the finding for entirely the wrong reason.
3. **Distinct strings at constant molecule size** (atom-map labels: `[NH2:1]N`, `[NH2:2]N`, …).
   This measures the code: 13 KiB of SMILES → 251,000 flags → 2.48 s of blocked event loop.

Either of the first two would have produced a confident write-up — one refuting a real defect, one
confirming it with a fabricated mechanism.

Rules:
1. **Before trusting a measurement, ask what the code does to your input before the part you are
   timing.** Deduplication, normalization, caching and truncation all silently change what you
   measured.
2. **Vary exactly one thing.** If the input's size and its cardinality both grow with `n`, the curve
   is not attributable. Constant-size distinct inputs are usually constructible — here, atom-map
   labels give distinct SMILES strings for identical molecules.
3. **A flat curve is a result that needs the same scrutiny as a steep one.** Probe 1 said "no
   defect" and was wrong; a negative result from a broken probe is indistinguishable from a
   negative result.
## A layering rule can decide where code lives better than taste can (2026-08-06, mounted file share)

Adding a share crawler needed the PDF/DOCX/XLSX/PPTX parsers that already existed in
`agent/attachments.py`. `tests/test_layering.py` forbids `ingest -> agent`, so the obvious moves
were both wrong: importing anyway (forbidden), or writing a second parser set (the duplication
CLAUDE.md forbids). The third option — move the parsers *down* into `ingest/documents/parse.py` and
have `attachments.py` import them — turned out to be the correct architecture on its own merits:
reading a PDF is an ingest concern that an upload happens to use, not the other way round. The
layering test did not obstruct the design; it named a misplacement nobody had noticed.

**Rule:** when a layering rule blocks a reuse, do not reach for a copy or an exception. Ask which
side is misplaced — the rule usually knows, and the answer is normally "move the shared thing down
to the layer that owns the concept".

## A counter that inflates is worse than no counter (2026-08-06, mounted file share)

A bounded crawl resumed from "the last accepted file". Everything examined-and-skipped between that
and where the chunk stopped was re-examined by the next chunk and tallied again, so a drain's
"skipped: .doc × N" numbers grew with the number of chunks. Nothing failed. The numbers were simply
read as a measurement of the share, and they were a measurement of the batch size. The cursor is now
the last entry *examined*, which costs nothing and makes the tally exact.

**Rule:** when a bounded/resumable pass reports counters, the resume point must be the last item
*examined*, not the last item *kept* — otherwise every skip between them is double-counted, silently,
on the success path. And when a counter is added, add the test that drives the loop in chunks of one
and compares the total against a single-pass run.

## A "mark" and its "sweep" must read the same clock (2026-08-06, mounted file share)

Deleting index rows for files gone from the share is a mark-and-sweep: each crawl restamps what it
saw, and the sweep removes rows older than the run's start. The mark was a database `now()`; the
start time was about to come from the Temporal workflow. A database running a minute behind the
worker would then make freshly-marked rows look older than the run that marked them, and the sweep
would delete files nobody had touched. The fix was one method — the index reports its own clock.

**Rule:** a comparison between two timestamps is only meaningful if both came from the same clock.
When one side is written by the database (`now()`, `DEFAULT now()`), the other side must be read
from the database too — never from the process doing the comparing.

## A cache key that is right in memory is right on disk too (2026-08-06, document re-embedding)

`core/embeddings.py` keys its in-process cache on `(provider, model, dim, text)`, with a docstring
citing D-011: a vector is only reusable for the configuration that made it. The document index then
stored those same vectors in Postgres and recorded none of that — so changing the embedding model
re-embedded nothing (the file fingerprint had not moved), and the table quietly held a mix of two
models' vectors. The rule was already written down, in the same repository, one layer up. It just
had not been carried across the boundary where the value became durable.

**Rule:** when a value is cached in memory under a composite key, ask what the *persisted* copy of
that value is keyed on. If the durable side keys on less, the extra key components name exactly the
change that will corrupt it silently. Store them.

## A remedy that needs you to already know is not a remedy (2026-08-06, document re-embedding)

The cheap fix for the stale-vector defect was a `--full` flag, or a line in the runbook saying
"drop the two tables after changing the model". Both are correct and both are useless, for the same
reason the defect was worth fixing: it raises no error. Nobody runs a repair step for a problem that
never announces itself, and the person changing an embedding-model setting is precisely the person
who does not know it invalidates a corpus.

**Rule:** when a defect's failure mode is silent, the fix must be automatic. A flag, a documented
procedure or a checklist item only closes defects that announce themselves — for the silent ones it
converts a bug into a bug plus a false sense of coverage.

## "The sandbox cannot run it" is a claim to test, not a limit to accept (2026-08-07, PR #143)

GitHub Actions was stopped account-wide, so #143 sat with zero check runs. `make cov` failed locally
at 82.26% against an 84.0% floor, and I recorded it as "environmental — 107 Postgres tests skip
offline" and prepared to hold the merge for a human decision. Two things were wrong with that.

First, I had asserted the cause rather than measured it. Running the same gate on the **base commit
already merged to `main`** gave 82.40% — also failing. That single number converts "my change may
have broken the floor" into "the floor is unreachable in this environment, including on `main`", and
it cost one command.

Second, the limit was not real. `postgres` 16 was already installed; the packaged pgvector was too
old for one operator class, and building 0.8.0 from source took a few minutes. With a real database
the same commit scored **84.59% — pass**, 3556 passed instead of 3449. The evidence I was going to
ask a human to substitute their judgment for was available the whole time.

**Rule:** before reporting a gate as unrunnable, try to make it runnable — check for the binary,
build the dependency, stand up the service. And when a gate fails for a suspected environmental
reason, run it on the unchanged base commit before saying so: "environmental" is a measurement, and
the base-versus-head delta is what turns it into one. Escalating to the user is the right move only
after the cheap paths are spent, not instead of them.

## A gate's red is a message, not a count (2026-08-08, the review-and-hardening campaign)

I recorded two `tests/test_pka.py` failures as "pre-existing on unchanged `main`", attributed them to
"an environment difference in the tblite numerics", and briefed **six** lane agents to ignore them.
All of it was wrong, and the evidence was in the output I had already produced:

```
E  Failed: Timeout (>180.0s) from pytest-timeout.
```

Not an assertion failure. The assertions never ran. Confirmed by lifting the cap: `--timeout=0` →
**2 passed in 1071.49 s**, on a box running four other agents, against a 180 s marker. Nothing about
a pKa value was ever wrong.

The cost was not the wasted red. It was that a false baseline propagated: the campaign ran for hours
against a gate state that did not exist, one lane spent its budget refuting my claim, and it reached
the *right* conclusion ("not a code defect") from the *wrong* evidence ("does not reproduce" — it
reproduces reliably, under load), which nearly buried the real finding a second time. The same root
cause turned out to explain every other red the campaign had written off as environmental:
`test_bo_constraints`, `test_bo_predict` ×2, `test_reizman` — six tests, one cause, hard
`@pytest.mark.timeout` markers that **override `--timeout` on the command line** and so cannot be
relaxed for a contended run.

**Rule:** read a failing test's *message* before characterising it. "N failed" is not a diagnosis,
and the two words that distinguish `Timeout` from `AssertionError` change what the failure means, who
owns it, and whether it is a defect at all. Never hand a "known failure" to another agent without the
failure text attached — a baseline is evidence, and evidence gets quoted, not summarised.

## A fix reopens the hole it closes, and only a review over the seam sees it (2026-08-08, same campaign)

Six lanes ran in parallel worktrees, each adversarially reviewed. Every review found roughly seven
real defects and **about a third were introduced by the fix under review**, repeatedly in the same
shape: the lane understood the defect class, fixed the named instance, and recreated it one step
away.

- The lane that moved attachment parsing off the event loop to stop a whole-pod freeze added, four
  files later, a 422 handler rendering `exc.errors()` — one error object per bad list element, 683,520
  errors and ~32 MB of response from a 2 MB body, on the same single worker, reachable by any
  authenticated caller.
- The lane whose ADR is titled *a partial answer must say so* shipped two truncation flags that were
  provably `len(hits) == cap` — the exact inference its own docstring condemns — so a complete scan
  announced itself as partial and a true negative rendered as inconclusive.
- The lane that fixed a retracted observation keeping its support made a *degraded corpus read*
  authoritative, rewriting evidence downward: support 3 → 1, measured.
- And I reintroduced the campaign's own ReDoS one rule over: `_NOT_MID_TOKEN` was added to bound a
  quadratic JWT pattern, and `_HAS_DIGIT` shipped as `_OPAQUE*\d` in a lookahead — 72 KB in 8.1 s,
  unauthenticated through the uvicorn access log, 21 s of held logging lock against a 10 s readiness
  probe.

None of these was visible to the lane that wrote it. Two were invisible to *any* per-lane run: a
`degraded(..., exc_info=False)` crash that only fires when an earlier test has installed the logging
filter, and a suite that was **red at HEAD while every lane's own subset was green**.

**Rule:** after fixing a defect class, grep the diff for the class — not the instance — before
claiming it closed. And a campaign of parallel lanes needs a review *of the seam*, run over the
combined diff with the whole suite, because a per-lane green is structurally unable to see an
interaction. Budget it as a phase, not as a formality.

## Delete the row in the commit that closes it — including your own (2026-08-08, same campaign)

`CLAUDE.md` says a `DEFERRED`/`BACKLOG` row whose work is done is deleted in the same commit, because
a row that outlives its closure reads as live state. I wrote a commit that removed both private MAF
imports, emptied `_KNOWN_PRIVATE_IMPORTS`, and **left the backlog row still saying "Two modules still
import `agent_framework._harness._loop`"** — and shipped no ADR for the decision at all. A critic
pass found both, three hours later.

Worse, the same pass found the campaign's "Refuted by measurement — do not re-open" list instructing
the next session to skip a *live* defect: "filtered-HNSW recall loss does not exist, because the index
was never used" had its premise falsified by a later lane that restored the index — and then measured
recall@10 = 0.116 uniform-random. A refutation is only valid against the tree it was measured on.

**Rule:** a commit that closes a tracked item edits the tracker in the same commit, and a "do not
re-open" list is re-validated whenever the code under it changes. Notes to the next session are
load-bearing: the failure mode is not an untidy file, it is a competent successor confidently
skipping the thing that matters.

## A stub blinds the test to the contract it stubs (2026-08-09)

Merging `origin/main` surfaced a total retrieval outage in `ExternalVectorDocumentIndex`: points
were grouped `doc_id@chunking_key`, the scope handed to `VectorStore.search` was bare `doc_id`, and
the intersection was empty for every query. Three tests covered the path and all three were green.

Two of them monkeypatched the scope function away. **A stub returns what it was written to return,
so it can never disagree with its caller — which means it also cannot detect that the real function
stopped agreeing.** The third called the real query but asserted only that a scope was computed, not
what shape it had.

Rules for myself:

- When stubbing a collaborator, ask what the stub makes *unobservable*, and make sure some other
  test observes it. Here the missing one is trivial: drive the real store end-to-end and assert the
  **score**, since a chunk queried with its own embedding must score 1.0. Content assertions would
  not have caught it — the content came back right, resolved from the catalogue.
- An identity change (id, key, group, cache key) is never local. Grep for every other place that
  *spells* the identity before reviewing whether the new spelling is correct, and prefer making the
  spelling one shared function so the drift becomes impossible rather than merely caught.
- "Returns empty" is a camouflaged failure: correctly-empty and catastrophically-empty are
  indistinguishable at the call site. Any function whose empty result is a legitimate answer needs a
  test that proves the non-empty case actually happens.

Third instance this campaign of the same shape: the defect sat one step from where the fix was
applied — sibling rule, neighbouring class, other half of a contract.

## A default that changes nothing is not worth a second rule (2026-08-09)

Building the tool-result store I gave `retention_tool_results_days` a 30-day default, on a sound
argument: the table holds no *record*, so unlike conversation history there is no GxP policy to
defer to the operator, and deferring it anyway means an unbounded table. Two tests then failed —
`test_retention_is_off_until_a_policy_is_stated` pins every window at 0, and the closed `_PRUNABLE`
set is asserted verbatim.

The second failure was the test working as designed: it exists to force a conscious update, and
updating it was the right move. The first was not. My instinct was to rewrite the guard with a
paragraph explaining why my table is special — and the measurement that killed that is one line:
`retention_enabled` is `False` by default, so on a default deployment 30 deletes exactly as much as
0 does. The two values differ only for a deployment that switched retention on and did not state
this window, which is precisely the case that test refuses. The whole argument bought nothing and
cost a second rule.

Rules for myself:

- Before weakening a guard test to fit a change, compute what the change actually buys **on the
  configuration that ships**. A principled default guarded by a feature flag that is off is not a
  default, it is a comment.
- Distinguish the two kinds of failing invariant test: one that enumerates a set (meant to be
  edited, deliberately) from one that asserts a rule (meant to be obeyed). Editing the first is the
  procedure; editing the second needs a reason that survives being measured.
- When the honest answer is "uniform rule, and the cost is real", write the cost where an operator
  will meet it — here `infra/sql/README.md`'s Disposal column says this table is unbounded until
  someone sets a window — rather than in the config comment nobody reads twice.

## Write over a file only after checking whether it already exists (2026-08-10)

Adding the LangGraph engine's tests I called `Write` on `tests/test_graph.py` — a path that already
held 23 tests for the NetworkX knowledge-graph indexer. They were gone, and **the suite still
passed**: a deleted test file does not fail, it simply stops asserting. `make lint type test` was
green across the destruction, twice.

What caught it was arithmetic, not the gate. The run before the change reported 3913 passed and the
run after reported 3897, while the change *added* six tests. A suite that shrinks when you add to it
is the whole signal, and it is only visible if the count is read rather than the word "passed".

The near-miss underneath it is worth as much as the mistake. `chemclaw.kg.graph` already exports
`build_graph`, so `agent/graph.py::build_graph` would have put two unrelated builders one import
apart — in a repository whose `ARCHITECTURE.md` exists largely to explain the name pairs that look
like duplicates and are not. The filename collision was the visible symptom of a naming collision I
had not thought about. Renamed to `agent/langgraph_agent.py::build_langgraph_agent`.

Rules for myself:

- **Before `Write`, check the path is new.** `Write` says "updated" rather than "created" when it
  overwrites; that word is the warning, and I read past it. For anything that might exist, `ls` or
  `git ls-files` first — one command against silently destroying reviewed work.
- **Read the test *count*, not the exit status, when a change adds or moves test files.** Compare
  `pytest --collect-only -q` across the change; `comm -23 before after` names anything lost. Green
  proves the tests that ran passed, never that the ones that should have run did.
- **A filename that is already taken is telling you the name is ambiguous.** In this tree "the
  graph" means the knowledge graph. Check what a name already means here before claiming it, rather
  than after the collision.
