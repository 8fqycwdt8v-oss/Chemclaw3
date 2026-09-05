# Lessons — the rules

**Read this whole file at session start.** That is only possible because it is short, and it is
short on purpose: it was 1,937 lines across 80 sections, which is not readable at session start, so
it was not read, so the rules did not fire. The proof is rule 1 below — the *same* `git checkout`
mistake is recorded five separate times in the old file, and the fifth entry says it plainly: *the
rule was already written down and it did not fire, so the rule is the problem, not the memory.*

Every rule here is one paragraph. The incident behind it — the measurement, what was tried first,
what it cost — is in [`docs/archive/lessons-2026-08.md`](../docs/archive/lessons-2026-08.md), which
keeps all eighty sections unedited, including the repeats. Go there when a paragraph is not enough.

**After any correction from the user**, find the rule this belongs under and sharpen it, or add a
new one if the lesson is genuinely distinct. Do not append a dated section — that is how the old
file grew. If a rule is being broken repeatedly, the fix is a *mechanism* (a script, a test, a
`Makefile` target), not a longer paragraph.

---

## Working the tree

1. **Never `git checkout <file>` to undo a mutation — copy the file aside and copy it back.**
   Recorded five times (2026-07-31, 2026-08-01, 2026-08-05, 2026-08-11, 2026-08-12) and it kept
   happening, so the rule is not "remember harder": `git checkout` discards *every* uncommitted
   change to that file, and during a mutation test the file usually holds work that is not
   committed yet. It also fails outright on a new untracked file (`pathspec did not match`), which
   in one helper meant the mutation was left in place and the "passing" run measured nothing. The
   mechanism: `cp $f $f.bak` before the mutation, `mv $f.bak $f` after, and never a git command in
   the loop. Related: a survived mutation is a *question* — read the patched line and confirm it is
   the invariant, then re-run the full suite, because two of five "coverage gaps" were mis-targeted
   patches (one hit a docstring occurrence).

   **And clear `__pycache__` inside the loop, or the loop lies to you in the alarming direction.**
   A `.pyc` is validated on the source's mtime *in whole seconds* plus its size, so a
   `cp`/edit/`mv` cycle that lands within one second and changes no byte count — swapping one digit
   for another, which is exactly what a numeric mutation does — leaves the stale bytecode looking
   current. Measured: a mutation loop reported a **failure on a restored, clean file**, because
   pytest ran mutated bytecode against restored source. It fails loudly rather than silently, which
   is the only reason it was caught; the same mechanism could as easily have run *clean* bytecode
   against a mutated file and reported a test as biting when it does not. `rm -rf` the relevant
   `__pycache__` (or `touch` the file) between the mutation and the run.

2. **`Write` to a path that already exists destroys it.** Calling `Write` on `tests/test_graph.py`
   deleted 23 tests for the NetworkX indexer, and the suite still passed — nothing referenced them.
   Check for the file first. A green suite does not notice tests that no longer exist.

3. **A stale local clone is not evidence about a remote.** I told the user four of my own pushed
   commits "do not exist" and that a sibling session's work was gone; all of it was on `origin` the
   whole time and the container's clone was behind. Fetch before asserting anything about what is
   or is not on the remote. **The same shape applies to a launch receipt: a tool result saying an
   async subagent started is not evidence that it is running.** Six triage agents were launched,
   all six returned "launched successfully", and a user interrupt in the same turn killed every one
   of them — I then reported "waiting on triage" across several turns while nothing was running.
   `ListAgents` is the check, and it costs one call; a completion notification that has not arrived
   is not the same as work in progress. Related: an environment daemon is not durable either —
   `dockerd` died twice unprompted mid-session, and every Postgres test would have *skipped green*
   rather than failed if it had gone unnoticed. Start it with `setsid` and re-check before
   trusting a suite result.

4. **The working tree is not your baseline when other agents are in it, and `git add -A` is a claim
   about a tree you no longer control.** A verification pass reported a feature "already
   implemented" while reading another agent's uncommitted work. Three substantive fixes were built
   twice by concurrent sessions on one branch. A review subagent mutating the tree had its mutations
   swept into a commit by `git add -A`. Diff against a named commit, stage by path, and treat
   another agent's own "gate is green" report as a claim, not evidence — one such package arrived
   five lines over the lint limit.

   **File ownership is the whole safety property when you fan out, so assign it like a lock: one
   writer per path, checked before launch.** Two agents were given `tests/test_template_agent_step.py`
   in the same batch — one to add tests, one to strengthen them. The second had been told to undo
   its mutations by `cp f f.bak` … `mv f.bak f` (rule 1, correctly), and its `.bak` predated the
   first agent's additions, so restoring its own mutation **deleted the other agent's new tests**.
   The source fix they proved stayed; the tests vanished; the suite went green at 35 passed, because
   a deleted test cannot fail. That is rule 2 arriving by a route rule 2 does not mention, and rule 1
   supplying the weapon. Before launching a batch, list every path in every prompt and check for a
   duplicate — a two-minute check that no amount of careful prompting substitutes for. When it does
   happen, the tell is a diffstat that omits a file an agent reported writing; `git diff --stat`
   over each agent's declared paths on completion catches it immediately.

5. **Run the gate's own command, at the gate's own scope, unpiped.** Pushed red twice for the same
   reason in two disguises: verifying a narrower scope than CI checks. And `| head` under
   `set -euo pipefail` exits 141 from a command that succeeded, while `grep … | head -10` hid the
   very match that refuted a "this config is dead" claim. Before asserting "never read", grep the
   identifier alone with no pipeline.

## Claims and measurements

6. **Measure it — an argument between two plausible mechanisms settles nothing.** Three of four
   re-opened refutations changed conclusion once counted rather than reasoned about; the retrieval
   leg everyone blamed contributed *zero* chunks. Before refactoring a hot path, benchmark it: of
   three obvious wastes, two were noise. A cost model fitted on 3–14-atom molecules gave the wrong
   exponent on the real 200–800 Da workload. An optimizer "improvement" was correct and useless
   twice before anyone timed it. A documented ceiling (`bo_max_rounds=500`) turned out not to be
   what bounded the thing it was documented as bounding. A high complexity score is a question, not
   a verdict. And a constant that stands in for missing physics gets swept, not assumed — 37% of
   directions were landing on a "safety net" floor.

7. **A measurement script is code, and an unrun one is a claim.** A cross-validation number
   travelled from an unrun script into an ADR and a maintained capability map. If a number is going
   to be quoted, the command that produced it must be re-runnable and must have been re-run.

8. **Analysing a finding is not fixing it.** I wrote exact diagnoses of two findings, including the
   fix each needed, and ticked both off; `git diff` showed neither file had been touched. Likewise a
   backlog row that says "this needs a decision" is a claim about the code — two such rows were
   simply wrong about what the code did. Check before deciding.

## Tests

9. **A green suite proves the code does what the test says, not that the test is right.** The
   recurring shapes: a test whose fixture is built through the code under test cannot fail; a test a
   *comment* can satisfy is a test of the comment; a fixture that hardcodes the value being measured
   buys the assertion; inverting a test is not rewriting it, and the inverted one usually stops
   testing; and a change whose test was *edited to fit it* passed a full gate twice. Verify a new
   test by breaking the code it guards. **Do this per fix, not per batch**: reverting four fixes
   together and seeing red proves only that *one* of the four tests works. Reverting each
   separately caught one of mine that passed both ways — a test for a wikilink spelled across two
   note blocks, which cannot happen because the blocks are joined by newlines and label prefixes,
   so the two `[` never meet. The test was deleted *and the docstring claim it came from was
   corrected*, because the invented justification had already been written into the code as fact.
   A test that passes both ways is not weak coverage, it is a false statement about the tree, and
   it usually arrives attached to a second false statement in prose.

10. **Green tests prove the paths you thought of.** Every defect in one heavy review sat in tested
    code — tested at the wrong layer. A stub blinds the test to the contract it stubs (a total
    retrieval outage hid behind one). A test can pin the *shape* of a control and never touch its
    effect — `test_harness_agent_still_audits_every_tool_call` passes with the audit middleware
    removed. And a test double's signature is untyped, so it drifts from the real one silently.

11. **A test that skips is not a test that passes.** An infrastructure skip is often removable —
    write the assertion where it can run instead. "It skips here" is not licence to migrate its call
    sites blind. "The sandbox cannot run it" is a claim to test, not a limit to accept. And check
    the negative case: `-p no:randomly` was a no-op, and a loop over an empty list asserts nothing.

12. **A gate's red is a message, not a count.** I recorded two failures as "pre-existing, an
    environment difference" and briefed six agents to ignore them. Read the failure before
    classifying it.

13. **A configuration only production sets is a configuration nothing tests.** The harness stack
    shipped in the Helm chart with a `False` code default, so 2,066 green tests exercised the other
    branch. Every production-only value needs one test that runs at the production value.

## Prose, docs and declarations

14. **The docstring is the best bug detector in a codebase that writes them.** Three of five real
    defects in a 12k-line review announced themselves in their own docstring. Read what a function
    claims and check it, rather than reading what it does.

15. **Prose is not covered by any gate, and it makes claims a test would refuse.** A rule written in
    three places is three rules, and a refactor's first job is to work out which of two copies is
    right — every finding in one review was a rule stated twice with a docstring asserting the other
    agreed. Where a document makes a checkable promise, make it a test instead of restating it.
    Three more instances on 2026-08-17, all found by opening the anchor rather than reading the
    prose: the runbook described a blocking `trivy` image scan that runs nowhere, `pyproject.toml`
    shipped a compiled engine to every pod that no module is *allowed* to import, and a config
    comment said an ENV knob "re-addresses every structure and therefore recomputes" when those
    bytes are what a *remote* server hashes — so changing it missed forever, silently. Each became
    a test. The tell they share: prose in the **present tense** about a control ("runs with
    `ignore-unfixed`", "drives the mass-balance check") is the highest-yield thing to go and check,
    because nobody writes a false sentence about a control they just looked at.

    **The counterpart is knowing when *not* to build the thing the row asks for.** The proposed
    mass-balance fix — products cannot outweigh inputs — is sound at any stoichiometry, and
    measured, no shipped outcome records a mass at all, so it would have run on nothing. A control
    that always passes is worse than a missing one, because it reads as coverage. Before
    implementing a check, confirm the data it reads exists.

16. **Delete the row in the commit that closes it — including your own.** A row that outlives its
    closure reads as live state; a status note appended under a stale row is how `DEFERRED.md` grew
    nine sections describing each other and `BACKLOG.md` reached 4,717 lines.

17. **A review's recommendation list goes stale before the review merges.** Several of one external
    review's 15 recommendations were already implemented by merge time — not superseded, done. Check
    each against `HEAD` before filing any of them.

## Fixing things

18. **The obvious implementation fails silently, and writing the rule down does not make it fire.**
    Three of five workstream items had an "obvious" implementation that produced a silently wrong or
    silently empty answer. I committed that exact sentence to this file and then shipped two more
    instances of it in the same branch's diff. The obvious *fix* for a real gap can also be worse
    than the gap — three items read as small and were mis-diagnosed by me when planning the fix.

19. **A fix reopens the hole it closes, and only a review over the seam sees it.** Across six
    parallel lanes, about a third of every review's findings were introduced by the fix under
    review. A predicate used as a filter must be safe to be *wrong* — the safety fix for two defects
    introduced a worse one. And a fix that passes every test written for it still needs the full
    suite: twice in one session that was the only thing that noticed.

20. **Generalize the defect before fixing the symptom, and remember that making it visible is not
    fixing it.** `standardize()` turning NaOH into water was one instance of a class that also hit
    `NaBH4` and `Pd(OAc)2`. A field added so a clean screen could name what it looked at made the
    gap visible and left it open. When a fit is bad, split the class before recalibrating — R² 0.50
    over 20 amines became a real finding once split by nitrogen class.

21. **A layering rule decides where code lives better than taste can.** `tests/test_layering.py`
    forbidding `ingest -> agent` ruled out the obvious move and produced the right structure.

22. **"Apply exactly once" is the wrong semantics for a reconciliation.** Privilege grants were
    filed as a numbered migration, which is applied once and tracked by checksum; a grant set has to
    be reconciled on every deploy.

## Signals that mislead

23. **Trust the output files, not the exit code.** `xtb --hess` computes a correct Hessian and then
    aborts with SIGABRT during teardown.

24. **A number that can only be wrong in one direction is worse than no number, and so is a remedy
    you have to already know about.** A resumed crawl re-counted everything it re-examined, so the
    counter only inflated. A mark-and-sweep whose mark read the database clock and whose sweep read
    the process clock deleted live rows. A join that is right most of the time attached the wrong
    blob the rest. A grading harness that defaulted to a verdict on its own failure reported 46%
    `unserved` and 36% `fabricated`, both wrong. And a cache key right in memory must be right on
    disk: the fix for a stale-vector defect cannot be a `--full` flag or a runbook line, because
    both require knowing the vectors are stale.

25. **Do not assert a sampled quantity, and do not assume symmetric physics gives a symmetric
    function.** CREST returned 2 conformers twice and 4 on the third run. `run_cached_interaction`'s
    docstring asserted A-with-B and B-with-A share a cache entry; they did not. Two backends must
    agree on the *physics*, not just the interface — one enabled a spin-polarization term the other
    did not, and triplet O2 came out above singlet.

## Changing shared things

26. **Widening or moving a capability breaks every layer that named the old one.** Widening
    `calc.pka` from acids to acids-plus-bases changed no signature and broke a consumer on another
    branch that had encoded the narrow domain. Moving seven calculators to an MCP server broke three
    things unrelated to chemistry. Two branches implementing one architecture rule nearly inflated
    every geometry by 1.8897 — check the invariant, not just the lines.

27. **A deletion is not verified by the type checker, and a guard's own corpus is part of the
    guard.** Five modules deleted, every removed symbol grepped, `mypy --strict` green over 635
    files — and five test failures, none of which the type checker could see. A conflict-marker scan
    matching seven characters and a space exempted git's eight-character markers, the exact case it
    existed for. Two checks over one corpus collide on each other's fixtures, and an ADR that names
    a counter-example will license it if the rule scans the whole body instead of the title.

28. **`git add <explicit paths>` does not bound a commit — the index does.** Three agents were
    working one tree with disjoint file ownership, and a commit staged by explicit path still swept
    in another agent's `git mv` renames, because `git mv` had *already staged* them and `git commit`
    ships the whole index. The other agent had run no `add` and no `commit`; its moves landed on the
    branch under someone else's message, half-integrated, while `README.md` and `CLAUDE.md` still
    named the old paths — which is how a mechanically correct rename turned a prose gate red. The
    mechanism: before a partial commit, read `git status --short` and check the staged column for
    anything you did not stage yourself, or use `git stash --keep-index`. Parallel agents make this
    routine rather than exotic: file ownership partitions the *working tree*, and the index is
    shared.

29. **Never pipe a test run through `tail` — the output *is* the diagnostic.** A full suite came
    back "2 failed" and the two names, and nothing else, because the command was
    `make test 2>&1 | tail -6`. No traceback, no assertion text, no timeout marker, so the failure
    could not be attributed at all and the only way forward was a second twenty-six-minute run.
    Redirect to a file and `tail` the *file* (`make test > run.log 2>&1`), which costs nothing and
    keeps the whole thing. The same mistake makes a *green* run untrustworthy for a different
    reason: skip counts and warnings are where a suite tells you it did less than you think.
    Related: do not run two suites at once. Two failures this session were xTB-backed tests under
    the 180s per-test cap while three pytest processes competed for CPU. **Resolved by
    reproduction, not by re-running until green**, which is the part worth copying: the two tests
    are deterministic (seeded embedding, no conformer search, GFN2) and collection order is fixed
    (no `pytest-randomly`), so both a numerical regression and order-dependent pollution would have
    failed *every* run. Saturating all four cores then reproduced the exact failure —
    `Failed: Timeout (>180.0s)` inside `tblite/library.py` — on exactly those two tests. Unloaded
    they take 12s against the 180s cap, so the suite is not marginal: 15x headroom, and it took
    full saturation to break it. There was no defect to file, which is why the elimination had to
    come before the instinct to file one.

    **The sharper consequence, hit again this session: a pipe replaces the *verdict*, not just the
    diagnostic.** `cmd | tail` exits with `tail`'s status, which is almost always 0 — so
    `uv run pytest ... | tail -2 && git commit ...` **commits over a failing suite**, silently, and
    the `&&` that looks like a guard is checking nothing. It did exactly that here: two
    `test_docstring_paths` failures were committed and only found on the next run. The rule is
    therefore stronger than "keep the output": never put a test or lint command on the left of a
    pipe when its exit code is load-bearing. Redirect (`> run.log 2>&1`) and read the file, or use
    `set -o pipefail`. A green line you read with your eyes is not a passing exit code.

30. **A one-token edit after the last green check is still an edit.** A merged sibling repo put the
    ported module one directory deeper, so a backticked pointer gained `engine/` — six characters,
    correcting a path to make it *more* accurate — and the docstring went from 99 columns to 105.
    Nothing was re-run, because the change felt like a typo fix rather than a code change, and CI
    went red on lint for a branch whose suite had been green minutes earlier. Then the reflow of
    that line pushed the *next* line over, twice, because each fix was checked by eye instead of by
    `ruff`. The mechanism: `ruff check . && ruff format --check .` is cheap and takes seconds — run
    it after the last edit, not after the last edit you *considered significant*. The category
    "too small to re-check" does not exist for a linter with a column limit.

## 2026-08-16 — a timing taken on a loaded machine is not a measurement

**Pattern.** Measuring `compute_thermochemistry` cold-vs-repeat through the new remote path, the
cold number came back at 115 s and then 147 s for ethanol against an in-process baseline of 0.816 s.
The obvious reading was a defect — a refinement loop that never converged, or a cache key that
missed every time — and I started instrumenting the loop to find it. There was no defect: two
abandoned background `pytest` runs were pinning all four cores at 124% CPU each. With the machine
idle the same call took 0.856 s.

**Rule for myself.** Before reporting or acting on any wall-clock number, check `uptime` and the top
CPU consumers, and kill anything I started in the background that should have finished. And where
the question is "was this recomputed?", assert the **call count**, not the clock: the count is
immune to load and is what D-011 actually claims. I did eventually measure it that way (`0 computed`
on the repeat) and that is the number that belongs in the report.

## 2026-08-16 — when a key is derived on the other side of a wire, ask what it names

**Pattern.** Three defects in one migration, all the same shape: the cache key is derived by the
*server* and the payload is stored by the *client*, so the client cannot see which arguments the key
names. `optimize_geometry` and `relax_structure` share one key and return different payloads; a
Fukui key does not name the mode; `multiplicity=None` means the opposite on the two sides. None of
the three raises anything — they produce a wrong answer, a stale ranking, and a refused embed with a
misleading message.

**Rule for myself.** When adopting a remote cache, do not infer the key from the tool's arguments.
Ask the server for the key of every *pair* of calls that could plausibly collide, and diff them —
`calculation_key` is cheap and the answer is a fact rather than an inference. Any two tools whose
keys are equal must return the same payload shape, and any argument the key does *not* name must
either be omitted from the request or re-applied locally after the cache.

32. **A lesson written down is not a lesson learned — #28 recurred, in the same session that could
    quote it.** Lesson 28 says `git add <explicit paths>` does not bound a commit, the index does.
    I ran `git add CLAUDE.md tests/pg.py` and committed a two-file docs change; the commit contains
    **38 files and 9385 deletions**, because a subagent had already staged its `git rm` of the calc
    engine and `git commit` ships the whole index. The commit titled "Record that the sandbox is not
    offline" now carries the deletion of twenty engine modules.

    What made it recur is worth more than the rule: lesson 28 is filed under *parallel agents*, and
    I did not think of myself as being in that situation — I was doing a small documentation fix
    while an agent happened to be working. The trigger is not "am I coordinating with others", it is
    **"is anything else able to write this index"**, and a running subagent always is. The mechanism
    is unchanged and cheap: `git status --short` before every commit, read the *staged* column, and
    if it holds anything you did not stage, use `git stash --keep-index` or commit from a worktree.

    Second-order: I decided *not* to rewrite the history, because every merge in this repo is a
    squash, so the misattribution never reaches `main` and rewriting a 101-file branch to fix a
    record that does not survive the merge is unnecessary risk. Put it in the PR body instead. The
    general form — **fix a record where the record will actually be read** — is the part to keep.

33. **Verify a claim at the layer the defect lives in, not at the layer that is convenient.** A
    subagent reported that the answer judge never ran, measured 8/8 against a live model. The model
    credential was exhausted by then, so re-measuring was impossible — but the *cause* was one line
    with no network: `convert_to_openai_tool(VerificationResult)["function"]["parameters"]` has
    `required == ["confidence"]`. Confirming that took one command and settled the question.

    The same move then produced a better test than the one I first wrote. My first attempt asserted
    the *fixed* schema required every field, which is false — `claims` has a default, so it is
    optional in pydantic's own schema too, and `method="json_schema"` buys strict provider-side
    enforcement rather than a different required-set. The failing assertion is what corrected my
    model of the fix. **When a test you wrote to prove a fix fails, consider that it may be telling
    you the fix works for a different reason than you thought** — before assuming the test is
    wrong.

34. **A null control is what turns "it helped 26% of the time" into a decision.** The brief for the
    answer-revision measurement asked for before/after scores and a substance check. A subagent
    added something I had not specified: re-score the *unchanged* answers, three more times. Doing
    nothing cleared the flag 5.1% of the time; revising and keeping the substance cleared it 5.1%
    of the time. Without that arm the honest write-up would have been "revision clears a quarter of
    flagged answers, most by deletion" — suggestive, arguable, and probably enough to justify
    building something. With it the answer is *zero measured benefit*, and the decision is made.

    **The rule: whenever a measurement scores an intervention against a stochastic judge, measure
    the judge alone on the same inputs.** The null arm costs one extra pass and is the difference
    between an effect size and an anecdote.

35. **Do not report a subagent's headline finding without probing it yourself — and when your probe
    disagrees, that is information, not a refutation.** The same agent reported the judge scoring
    `1.0, 0.0, 1.0, 0.5, 0.5` on five identical calls, which would make `review_required` noise. Two
    probes here — one trivial, one a realistic multi-claim fully grounded answer — both returned
    1.00 six times out of six. Neither of us was wrong: the judge is stable where the answer is
    unambiguous and unstable at the margin, which is exactly where the 0.7 threshold sits, and the
    null control had already measured that margin at 5.1% per roll.

    Reporting either number alone would have been misleading — "the judge is unreliable" overstates
    it, "I could not reproduce it" buries it. The characterization that survives contact with both
    observations is narrower and more useful than either.

36. **"Run the full suite" is per repository, and the second repository is the one you forget.**
    Adding `revision` to the mcp fleet's `/healthz` payload, I ran `ruff`, `make type` and
    `uv run pytest -q tests` — the fleet directory, where my new tests lived. CI went red on five
    per-server `test_healthz_answers_and_names_the_server`, each an exact dict comparison, all of
    which a bare `uv run pytest -q` would have shown in 40 seconds.

    This is not lesson 29 (a `tail` swallowing an exit code) or lesson 30 (skipping `make type`).
    It is narrower and it is specific to this family of four repos: I had *just* run the full
    Chemclaw3 suite, and the discipline did not transfer across the `cd`. The path-scoped run is
    also what made it feel complete — the subset I chose was exactly the subset containing my new
    tests, which is the least informative one available.

    **The rule: a change to shared infrastructure gets the repository's whole default test command,
    unscoped, before the commit — and when the change touches a payload, a signature or a schema,
    assume the assertions that break are in files you have never opened.** `packages/` in that repo
    is imported by all five servers, so "I edited one file in `packages/`" is exactly the case where
    a scoped run proves the least.

37. **`git checkout <file>` is how you lose a mutation-check's subject.** Verifying a fix by
    reverting it and watching a test go red is the right discipline, and I used `cp` to a backup for
    four of five checks. For the fifth I reached for `git checkout src/.../live.py` to restore — and
    the file's fix was *uncommitted*, so the checkout restored HEAD and silently deleted the work
    the mutation was testing. The follow-up "restore" script then found no mutation to undo and
    printed success anyway.

    The tell was there and I nearly missed it: the test stayed red after the "restore". Had the
    assertion been weaker, the defect would have gone back into the branch under a green line.

    **The rule: restore a mutation from a copy you made yourself (`cp file /tmp/x.bak` → `cp back`),
    never from git, unless the file is committed.** And a restore script must *assert* it found what
    it was undoing — print-on-success outside the conditional is how a no-op reports as a fix. The
    same assert-the-target rule already applies to applying a mutation (a ruff reflow once made one
    silently not apply); it applies just as hard to undoing one.

38. **A test with a timeout is a timing measurement, and running two repositories' suites at once
    invalidates it.** `tests/test_reizman.py::test_bo_campaign_finds_high_yield` failed on a
    `Timeout` in a full run. I had started `Chemclaw3-mcp`'s suite concurrently on the same four
    cores. Alone, the same test passes in 68 s.

    `tasks/todo.md` already carried this as a failed approach — "a wall-clock number taken while the
    test suite is running is not a measurement" — recorded after two abandoned background pytest runs
    made a 0.8 s calculation look like 147 s. What is new is only that it now reaches *test outcomes*
    rather than reported timings, and that the contending load came from a sibling repository, where
    I was not thinking about this machine's cores at all.

    **The rule: before treating a timeout or a slow-test failure as a finding, check `uptime` and
    re-run it alone.** And do not start a second repository's full suite while one is running — the
    time saved is not real, and a false failure costs more than the wait.

39. **A check that has never run is not a passing check, and turning it on is a change with
    findings.** I added `fetch-depth: 0` as a one-line CI fix — the migration-immutability test could
    not run on a depth-1 clone, where every file compares equal to itself. My local suite stayed
    green because *this sandbox's clone is also shallow*, so the test skipped here too and I never
    saw its first real execution.

    CI did, and it found two migrations whose `CREATE TABLE` had been edited after merge. That is the
    check working on its first run, not a regression — but I had shipped the enabling line as though
    it were free, and it was not: it was a change whose whole purpose was to surface something, and I
    did not go looking for what.

    **The rule: when you enable a check that was previously inert, run it locally under the
    conditions that make it real *before* pushing** — here, `git fetch --unshallow` first. And expect
    a finding: a guard nobody has been able to violate-and-fail against has, in this repo's
    experience, always had something behind it.

40. **This sandbox's clone is shallow, and the migration-immutability check reads that as a
    finding — recognize it instead of re-diagnosing it.**
    `tests/test_migrations_are_additive.py::test_no_grandfathered_edit_outlives_its_reason` reported
    `002_molecule_fingerprints.sql` / `003_reaction_fingerprints.sql` as exemptions with nothing left
    to permit. The cause is lesson 39's, seen from the other side: nothing differs from the commit
    that introduced it when the history is not there.

    **What makes it easy to misread is that it presents two ways depending on how deep the clone
    happens to be.** At 170 commits the `compared < 30` skip guard did not fire, so it *failed*.
    After merging `origin/main`, only 8 migrations could be compared, the guard fired, and the same
    check *skipped*. Same cause, opposite symptom, and neither is about the code under review.

    **The rule: when a suite comes back with exactly one failure that is nowhere near what you
    touched, stash and re-run before reading a line of the diff.** It cost me one round here and it
    is a two-command check: `git stash -u && pytest <the one test> && git stash pop`. Then say in the
    PR that it is pre-existing *and how you verified that*, because "unrelated" asserted without the
    stash is indistinguishable from not having looked.

## A line count is not a measure of reading cost (2026-08-17)

**What happened.** Asked to make the agentic backend "lean to read", I profiled it, found `agent/`
at 58% prose (11,217 lines, 2,895 of them code) and a 165-module import fan-out, and proposed three
fixes — one structural, two derived from those two ratios. The user approved all three. Both
ratio-derived ones then failed on measurement: relocating the biggest docstring yielded **0 lines**
once readability was held constant, and the best available import work reached 37% only by moving
eight dependencies into function bodies, which makes the tree *harder* to read. The structural one
— a single 483-line function — was the entire real problem and went to 194.

**The rule.** Measure a candidate *before* pitching it, not after it is approved. A ratio is a
smell, not a finding: prose that records a measurement is not overhead, and this tree's long
docstrings are mostly that. What actually costs a reader is structure — a function too big to hold
in your head, a duplicated loop, a rule stated as a comment where it could be a type.

**The tell I should have caught.** I had already measured the docstring distribution — median 9
lines, mean 12.8 — which says the prose is healthy and the mass sits in a tail of deliberate
records. I read that number and still proposed a mass relocation, because the *total* looked large.
Medians answer "is this bloated"; totals do not.

## A green readiness route is evidence about connectors, not about dependencies (2026-08-17)

`/readyz` reported every connector `healthy` while two things were broken underneath it, and I
believed it both times before measuring:

- `chem`/`safety` read `healthy` because `/healthz` is unauthenticated. The front door held no
  token for them, so every `/mcp` call was rejected and turns degraded with nothing naming a
  credential. A health route that does not exercise the credential cannot report on it.
- The `calc` server was down and `/readyz` was *entirely* green, because `calc` is dialled from
  inside a tool rather than probed as a connector. Every calculator tool failed at call time.

**Rule for myself:** when a probe says a system is healthy and a turn says otherwise, the turn is
the measurement and the probe is a claim about a narrower thing than I assumed. Find out exactly
what the probe covers before using it as evidence — and specifically, never infer "the dependency
is up" or "the caller is authorized" from a health route.

## Do not accept a subagent's ranked root causes without checking the mechanism (2026-08-17)

A subagent returned three confident, well-formatted candidate root causes for the UI defect, all
three built on the premise that zustand's `persist` middleware suppresses subscriber notification
for non-persisted fields. It does not — `partialize` decides what is written to storage, not who
gets notified. Every candidate was wrong, and the real cause was two layers away (`http-proxy`
emits `proxyRes` before copying headers, so an early `flushHeaders()` silently voids the copy).

What the subagent *was* good for was the inventory it gathered along the way — zustand version,
every selector expression, the exact reducer code. **Rule for myself:** take facts from a subagent,
take its conclusions as hypotheses, and check any mechanism claim against how the library actually
behaves before building on it. This is the same failure mode CLAUDE.md already names for prose —
articulate is uncorrelated with true — and it applies to my own subagents too.

## Start the long gate early, and never `pkill -f` a pattern that matches my own shell (2026-08-17)

Two process mistakes in one session, both cheap to avoid:

- I ran `make test` under a 900 s `timeout` and reported the result as a failure. Exit 143 is
  SIGTERM — *my own timeout*, not the suite. The full suite is 4,178 tests and takes hours here
  with Docker up. **Rule:** check whether a non-zero exit is 143/144 before calling anything
  failed, and start a known-long gate in the background at the *start* of a session, not the end.
- `pkill -f "pytest -q"` matched the bash wrapper the harness runs my own commands in, killing my
  waiters (exit 144). **Rule:** kill by pid from `pgrep -f "python3 -m pytest" | head -1`, never by
  a loose pattern that my own command line also contains.

## "Not answering" is a claim about the network; check the server's own log (2026-08-17)

Four storm checks failed with `CalcServerError: the calculation service is not answering`. I was
one step from filing a Temporal durability defect. The server's access log said `401 Unauthorized`
— it was answering every single call, and refusing the credential.

Two rules from it. **When an error message names a subsystem's state, verify that state at the
subsystem**, not from the message: the message is the caller's belief, and here the caller could
not tell a refused connection from a refused credential. And **a retryable error class is an
assertion that waiting helps** — misclassifying a 401 as an outage does not just mislead a reader,
it spends the whole retry budget proving the same thing.

I also killed my own shell with `pkill -f` a second time, after writing the rule not to. Use
`pgrep -f <pattern> | head -1` and kill the pid.

## `x or default` erases a legitimate zero, and I shipped it into my own checker (2026-08-18)

The first script written to verify the seeded corpus scored `abs((rx.yield_percent or -1) - want)`
and reported 21 of 400 records as yield mismatches. There were none. `0.0 or -1` is `-1`, and 236
of the 3,955 published Buchwald-Hartwig wells are exactly 0.00% — a *real* result meaning that
combination failed, not a missing one.

I nearly filed it as an adapter defect. What caught it was that 21/400 ≈ 5.3% matched the corpus's
own 6% zero-yield rate too well to be a coincidence.

**Rule:** never use `or` to default a numeric that can legitimately be zero — `is None`, always.
And when a defect rate looks suspiciously close to some *proportion of the data*, suspect the
measurement before the system. The lane now pins this invariant in a test, because the bug is one
keystroke away and silent in both directions.

## Measure the behaviour a fix depends on, even when the code reads clearly (2026-08-18)

I re-targeted a probe (`gr-03`) from an unreachable dataset to a reachable one, on the assumption
that *reachable* implies *findable*. Then I noticed the ingested notes were unmerged proposals — 39
note files in the knowledge repo against 2,000+ ingested reactions — and that
`FingerprintReactionRetriever._eligible` drops matches whose note is not on disk. That reads like
the fix does not work.

Running it showed **both** are true, depending on the path: an unfiltered `similar_reactions`
returns 10 real wells, and the same search narrowed by `{"type": "reaction"}` returns 0. The probe
names the unfiltered tool, so the re-target holds — but I only know that because I ran it, and the
opposite conclusion was equally available from reading either function alone.

**Rule:** a fix that depends on a system behaviour is not done until that behaviour is measured,
however clearly the code reads. Two correct functions can compose into a consequence neither
docstring states.

## Read the bound; do not extrapolate the counter (2026-08-18)

I estimated a corpus drain at ~43 chunks from `ingested=100` log lines and told the user so. The
real number was ~108: `_BoundedIngest` caps a chunk at 100 *entries fetched*, not 100 ingested, so
the drain walks all 10,011 exports rather than only the 4,251 that map. The counter I extrapolated
from was reporting a different quantity than the one that governs the loop.

Related, same session: I grepped a background log for an auth error, found none, and briefly
concluded a test had failed for a different reason. The capture had been piped through `tail -12`
and the traceback was cut. **Rule:** a conclusion from a truncated capture is not a measurement —
re-run the specific thing.

## 2026-08-21 — a review is not finished until you have read the other side of the wire

**The pattern.** I published a deep review of how information moves between agentic steps, and it
was right about the shape of the problem and wrong or incomplete on five specifics — every one of
which I found in twenty minutes of reading `Chemclaw3-mcp` and running one more measurement:

- I named `predict_site_reactivity` as a place to accept a geometry. The calculation server has
  `compute_properties_at` and **no `compute_fukui_at`**, so the argument would have been a promise
  this repository cannot keep.
- I named `QmJobSpec` first in the recommendation and used it as the worked example. Its geometry
  contract lives in a Nextflow pipeline on a cluster, and Nextflow *silently ignores* a param no
  process consumes — so the recommendation as written would have shipped a silent wrong answer.
- I called `sample_conformers` the worst payload. `find_calculations` is **28x worse**
  (~831,000 tokens against ~7,400) and I had not measured it.
- I did not notice that `calculation_key` already returns `structure_id` and the client drops it —
  the exact fact that made the whole fix cheap.
- I did not notice that the server's `Structure.structure_id` is a `computed_field` and ours is a
  plain property, so the authoritative address arrives on every payload and is discarded.

**The rule.** *When a finding is about a boundary, read both sides before writing the
recommendation.* Four of the five errors were the same error: reasoning about a contract from one
end of it. The companion repos are two minutes away (`add_repo` + `git clone`), and this repository's
CLAUDE.md says so in its own "Related repositories" section.

**The second rule, which is older and I broke again.** *Measure the tool you did not think of.* I
measured five durable job payloads carefully and never ran the one read-only tool that could return
fifty of them. The prompt "which surfaces return a stored payload of unbounded size?" would have
caught it; "how big is this result?" did not.

## 2026-08-21 — a shape test proves the field exists, not that anything fills it

**What happened.** `ConnectorJobResult.calc_refs` was added, the collector was written, the field
was on the envelope, and a test asserted the envelope carried what it was given. All green. The
first time I drove the actual chain end to end, a conformer job that had plainly reached a cached
calculation reported **`calc_refs: []`** — the one line that records a key had failed to land,
because a scripted string replacement matched a fragment `ruff format` had already reflowed.

**Two rules, and the second is the one that generalises.**

1. **A scripted edit that does not `assert` its target is an edit that may not have happened.**
   Every `str.replace` in a batch script needs `assert old in s` before it, or a grep after it.
   I asserted most of them and not that one, and that is exactly the one that silently vanished.
   The cheap systematic check is a grep audit at the end: one line per intended change, printing
   OK/MISS. It found nothing else — but it could only say so because it was run.

2. **A test that constructs the model proves the shape; only a test that runs the code proves the
   wiring.** `test_the_envelope_carries_the_calculations_a_note_would_cite` builds a
   `ConnectorJobResult(calc_refs=[...])` and asserts it round-trips. It cannot fail on a missing
   producer, and it did not. The replacement drives `run_xtb_calculation` and asserts the refs are
   *non-empty* — a property of a run, so the test has to be a run. Whenever a change adds a field
   that something else is supposed to fill, at least one test must exercise the filler.

## 2026-08-21 — "targeted runs passed" is not "the suite passed"

**What happened.** I reported the gate as lint-green, type-green, and the suite "still running,
every touched area passed on targeted runs". The full run then found **three** failures I had
caused or exposed, none of them in a file I had thought to run:

- `test_calc_remote` asserted on `remote_key`'s return, whose *type* I had changed. I ran the calc
  tools, jobs, compose and find tests — not the one named after the module I edited.
- `test_layering` needed the new `cli -> science` edge declared. A structural test, invisible to
  any per-feature run.
- `test_suite_timeouts` failed only under `PYTEST_TIMEOUT_SCALE=4` — which is what the suite's own
  timeout banner tells you to set. A pre-existing hermeticity bug that only bites the person
  following the advice.

**The rule.** *A change that alters a function's signature or adds an import edge has a blast
radius no per-feature test selection covers.* Two cheap checks close most of it before the full
run: `git diff --name-only | sed 's|src/chemclaw/|tests/test_|'`-style name mapping to find the
test file named after each edited module, and running the structural suite —
`test_layering`, `test_repo_map`, `test_schema_inventory`, `test_database_privileges`,
`test_decision_log`, `test_prose_contract`, `test_docstring_paths` — on every change, because those
fail on the *shape* of a diff rather than on its behaviour.

**And the honest-reporting half.** Saying "green" before the run finishes is a claim about the
future. The right sentence is the one I used — "still running, I will report exactly what it says" —
and then actually reporting it, including the three that were mine.

## 2026-08-21 — never `cd` inside a compound command that also runs git

**What happened.** To check whether two failures were pre-existing, I made a worktree at the base
commit:

    git stash -u -q && git worktree add -q /tmp/basecheck e5f1f67 && cd /tmp/basecheck && ln -s …; git stash pop -q

The shell's working directory **persists across the whole command**, so `git stash pop` ran from
`/tmp/basecheck` and applied my three uncommitted test fixes to *that* worktree. `git worktree
remove --force` then deleted them. The next commit carried only the lessons file while its message
described three fixes that were no longer in the tree — and `git status` had told me so in one line
I read past.

**Three rules, in order of how much they would have saved.**

1. **Use `git -C <path>` instead of `cd`.** Every git subcommand takes it, it cannot leak into the
   next command, and it makes the target explicit at the call site.
2. **Never stash across an operation that changes worktrees.** A stash is repository-global and
   pops into whichever worktree asks. Committing to a scratch branch, or just reading the base
   version with `git show <rev>:<path>`, has no such failure mode.
3. **Read `git status --short` before writing the commit message, not after.** It printed exactly
   one file where I expected four. The message I then wrote was a description of intent rather than
   of the diff — which is the worst kind of commit message, because it reads as verified.

The generalisation, and it is the same one as the silent `str.replace`: **an edit is not done
because you made it — it is done because you checked it is there.** Both losses this session were
invisible for the same reason, and both were one `grep` away.

## 2026-08-25 — measure the mechanism, not only the outcome

Four defects in one session were invisible to reasoning and obvious to a five-line measurement.
Each had a plausible argument behind it that was simply wrong.

- **A de-overlapping rule inferred from content.** "Strip the longest repeat, bounded by
  `overlap_chars`" is correct-sounding and deleted 2,400 of 5,000 characters on a repetitive line.
  Adding a one-character periodicity shift fixed that case and still deleted 2,800 of 6,000 on
  period-10 content. *Rule for myself: when a rule infers a boundary from content, generate the
  adversarial content before writing the rule — repetition, periodicity, and the empty case.*
- **A payload that said everything twice.** The condensation returned the rendered table and the
  rows it was rendered from. The design read fine; the number was 1.4x, which would not have been
  worth building. *Rule: measure the thing the change exists to improve, before believing it
  improved.*
- **Two orderings of one list.** `rows` came back in input order while the renderer sorted
  internally — so a column that says "changed vs previous" would have been a claim about a
  different row. Caught by a test asserting the returned order, not by reading the code.
- **A test that passed against the mutant it was written to catch.** The starvation guard asserted
  only that one source survived; the shape it was guarding against starves the *other* one.
  *Rule: after writing a regression test, break the code the way the test describes and watch it
  fail. If it does not, the test is documentation.*

The generalisation, which the repository already says and I had to relearn by doing: **prose is
evidence about what its author believed, never about what the code does.** Three of these four had
a docstring or a comment asserting the correct behaviour at the moment the behaviour was wrong.

## 2026-08-25 — do not answer a second problem as a side effect of the first

Building the condenser surfaced `read_corpus`'s full-rescan of the ELN. A derived store of mapped
`OrdReaction`s would have closed it — and would also have been the easiest way to give the
condenser its structured fields. Two problems, one store, and the store would have been built
without anyone deciding to build it.

Note frontmatter answered the condenser's need with no new store and no migration, and the rescan
is now a `BACKLOG.md` row with its own anchor and its own trigger. *Rule: when one change would
close a second, unrelated problem as a side effect, that is a signal to check whether the second
problem is driving the design — and to file it rather than ride it.*

## 2026-08-25 — a fixture that never varies is a test that never tests

Reviewing my own merged diff found four defects, two of them producing confidently wrong output.
Every one had a test nearby that passed, and every one got through for the same reason: **the
fixtures never varied along the axis that broke.**

- Every condenser fixture was a reaction note with the same fields. The heterogeneous case — a
  share document beside reaction notes — fabricated four condition changes.
- Every fake client always succeeded. One failing extraction fabricated two solvent swaps.
- Every budget test used chunks with default provenance. A chunk carrying conflicts and a real
  source label was charged 47% less than it costs.
- Every document read was of a document that fits. An oversized one fetched all 16 of 16 pieces
  past a ceiling whose comment says it prevents exactly that.

*Rule for myself: for each new test, name the axis the fixture holds constant, and ask whether the
code behaves differently at the other end of it. Absent-vs-present, fails-vs-succeeds,
small-vs-over-the-limit, homogeneous-vs-mixed — those four axes account for all four defects.*

The sharper lesson is about where the knowledge already was. `changes_between`'s docstring names
the absent-is-not-a-value hazard exactly, and excludes fields for it. I read that docstring, quoted
its reasoning into `_changes`'s own docstring about reagents — and then wrote the unsafe comparison
for the three columns immediately below it. **Citing a rule is not applying it.** When I find myself
writing "this is the hazard X avoids", the next step is to check that the code I am writing avoids
it too, not to treat the citation as the check.

And: `tasks/lessons.md`'s previous entry — measure the mechanism, not the outcome — was written in
the same session as the code that failed it four times. A lesson recorded is not a lesson applied.

## 2026-08-25 — a plan that says "zero new code" is a claim, and mine were wrong twice

**What happened.** Planning the Databricks work, I wrote two confident structural claims into the
plan and both were false:

- *"Pistachio is zero new code — one manifest."* The `vector:` half of the warehouse binding runs
  `VECTOR_COSINE_SIMILARITY(col, ?::VECTOR(FLOAT, n))`, which is Snowflake's function and Snowflake's
  type. Databricks has neither, and — the part I would not have guessed — no array *parameter* type
  at all, so a 1536-float query vector cannot be bound as a list on any statement.
- *"The vendor shapes go in `tests/test_upstream_surface.py`."* That file's assertions import their
  package unconditionally and its version floor calls `version(package)`. These clients are
  deliberately not installed here, so entries there would have made the suite depend on them.

Neither survived contact with the file. Both were plausible because I had read the *neighbourhood*
— the seam's README, the sibling adapter — and inferred the rest.

**The rule.** Before writing "no change needed to X" into a plan, open X and read the specific lines
that would have to hold. A README describes intent; the function body is what runs. For a *test*
file, read its docstring's statement of what belongs in it — three of this repository's test files
say so explicitly, and one of them said the opposite of what I planned.

**The second rule, which is the more expensive one.** I nearly shipped the Databricks score straight
through as a cosine. It is `1/(1 + d²)` over *Euclidean* distance, and `VectorMatch.score` is
contractually a cosine that the fusion layer ranks on. Nothing would have raised; a corpus would
just have been ranked slightly wrong forever. **When adapting a vendor to a numeric contract, look
up what the number actually means, and write down the boundary values** — identical, orthogonal,
opposing. Three lines of arithmetic turned an assumption into a test.

**And the thing that made all of it visible:** a review pass over my own plan, run against the real
files rather than my memory of them, before writing any code. It found five real problems, of which
I had independently caught three. The two I had not were the two that would have shipped.
---

## 2026-08-25 — Test against the real model, not against your reading of it

Building the result-publication projectors, I read `science/calc/models.py` carefully, wrote
seventeen projectors from that reading, and then ran them against real model instances. Three
things I had "verified" by reading were wrong:

- `Conformer` has no `energy_hartree` — only `EnsembleMember` does. My projector required it, which
  would have made every *returned* ensemble unpublishable while every cached one worked.
- `EnsemblePayload` has no `smiles` — it is keyed by `structure_id`. The subject builder raised.
- `DescriptorProfile` has `fraction_csp3`, which I had simply not seen, and so did not publish.

Each took one execution to surface and would have taken a long time to find in production, because
the failure mode of the third is *silence*: a field nobody publishes looks exactly like a field
nobody has.

**The rule: a projector is not written until it has been run against an instance of what it
projects.** `model_fields` is one line and a constructed instance is three; that is cheaper than
any amount of re-reading, and it is the only thing that distinguishes a field you decided not to
publish from one you never noticed.

**The stronger form, which is what I should have started with.** Reading catches what you look for.
A *coverage check* catches what you did not: wrap the payload in a dict that records which keys were
read, run the projector, and diff the read set against the model's fields. That found three more
gaps — including an exotherm boolean published without the threshold it was judged against, which
would have been uninterpretable the moment an operator changed the setting. It is now
`tests/test_publish_projection.py::test_every_model_field_is_read_or_deliberately_ignored`, with an
explicit exemption list so a deliberate omission carries its reason and an accidental one fails.

The same shape generalizes: **wherever one model is projected into another, assert the mapping is
total or explicitly partial.** A partial mapping nobody declared is indistinguishable from a
complete one, right up until someone asks for the missing half.

## 2026-08-25 — A position-matched zip between two independently produced lists

`ReactionEnergyResult` carries `reactants`/`products` and, separately, a `species` list. I zipped
the second onto the members built from the first by index. They are produced independently — a
`quick`-level run returns *no* species at all — so a two-species breakdown over a three-member
equation attached cyclohexane's free energy to butadiene.

What makes this worth recording is that it is **silent by construction**: both values are plausible
energies in the same units, on the same reaction, so nothing downstream — not a type, not a range
check, not a reviewer's eye — would have caught it.

**The rule: never zip two lists by position unless one is documented as derived from the other.**
Match on identity. And where a test can distinguish the two, make the fixture *disagree* on order
deliberately — mine now lists its species product-first, so a reintroduced index match fails
immediately rather than passing on a coincidence.

## 2026-08-25 — take the number off the wire, not off a serializer you chose

I measured the condenser's saving with `model_dump_json()` and shipped the figure in a commit, an
ADR and a PR body. Production never calls it: LangChain's `_stringify` tries `json.dumps`, fails on
a pydantic model, and falls back to `str()`. The real saving was **2.7×**, not 9.1× — and the
`Field(exclude=True)` the measurement was built on had no effect at all.

The tell was available the whole time and I did not look for it: I never once read a `ToolMessage`.
Every measurement went through an object I built and a serializer I picked.

*Rule for myself: when measuring what something costs a model, obtain the bytes from the production
path — drive the compiled graph, read the message it produced — and never from a representation I
selected. If I cannot name the function that turns my return value into what the model sees, I have
not measured it.*

This is the same error as the previous entry, one level up. There I charged `content` instead of the
serialized chunk; here I serialized with the wrong function entirely. Both are "I measured the
mechanism I assumed was running." The previous entry's rule — measure the mechanism, not the outcome
— was necessary and not sufficient, because I did measure a mechanism. It was the wrong one, and
what distinguishes the right one is that **something else in the system actually calls it.**

An honest note on sequence: three review passes over the same diff found three defects of this
family, each after I had written a lesson about the family. Recording a rule and applying it are
different acts, and the second one has to happen at the moment of writing the code, not afterwards.
## 2026-08-25 — a check that skips is not a check that passes

**What happened.** I edited migration `050` after committing it, to add a column. Locally
`tests/test_migrations_are_additive.py` was green; CI failed on it. The test *skips* on a shallow
checkout ("truncated history: ... this check would compare files against themselves") and runs
under CI's `fetch-depth: 0`. I read the green line as "this passed" when it said "this did not
run".

The same session had already stated the general form of this — CLAUDE.md's "never report a local
run as green without saying what it skipped" — and I applied it to the Postgres tests I *knew*
about while missing the one that announced itself in the skip reason.

Worse, the defect had already shown itself: applying the edited `050` broke `make db-migrate` on my
own dev database with "was edited after being applied". I reset the table by hand and moved on. The
error message was the test's message, one layer down, and I treated it as an environment chore.

**The rule.** When a local run is green and CI is red on the same commit, suspect a *skip* before
suspecting the environment — read the skip reasons, not just the count. And when a local command
fails in a way that needs a manual workaround to proceed, that workaround is evidence about the
change, not a chore: ask what the failure is telling you before undoing it.

**Concretely for this repo:** `git fetch --unshallow` before trusting
`test_migrations_are_additive`, `test_no_merged_migration_had_its_statements_changed` or anything
else whose skip mentions truncated history.

## 2026-08-26 — I tested the mechanism I wrote, not the one that calls it

I shipped a publish seam whose headline claim was "every composite reaches the results store", and
the composite path published nothing. All four shipped jobs resolved to no projector. The suite was
green: 72 publish tests, four files, every result shape round-tripped.

Every one of them started at `project()`. They passed `payload_kind="ReactionEnergyResult"` by hand
— and no production call site set `payload_kind` at all. One test file called
`records_from_solvent_screen()` directly; nothing else in the tree called it. `grep` for the
composite hook across `tests/` returned zero hits.

So the suite proved the projectors work. It said nothing about whether anything reaches them, and
that was the only interesting question.

This is the previous entry's rule one level up, and I want to be precise about why I missed it.
That entry says *measure the mechanism, not the outcome*. I did measure a mechanism. A projector
**is** a mechanism — it just isn't the one under test when the claim is about a path. What makes a
mechanism the right one is not that it is concrete, it is that **something else in the system calls
it**, and I get to choose my test's entry point exactly the way I got to choose that serializer.

*Rule for myself: a test of a seam starts at the outermost thing production calls — the envelope a
job returns, the row a walker reads — never at the function I am proud of. If I cannot name the
production caller of the function my test invokes first, I have tested my own intentions.*

The cheap check that would have caught all of it, in one line:
`grep -rn "<the hook>" tests/` — if the hook has no test, the feature has no test, whatever the
count of green assertions downstream says.

**And the corollary, which cost me two more defects before I learned it.** Fixing the nine did not
prove the path worked. *Assembling* it did — and it failed twice before it passed, on two things
that make the feature completely unusable and that no unit test could have seen:

- The one driver I ship failed the one sink I ship, because `Warehouse` is `@runtime_checkable` and
  a runtime Protocol check tests for the presence of **every** member. Mine was missing one it had
  no use for. Every delivery died at the connect.
- Every drain pass leaked a database connection, because "build the sink per run" and "hold the
  connection for the sink's life" are each correct alone and nothing closed the sink. Four an hour
  against a default `max_connections` of 100.

Both are invisible to a test that delivers to a stub and to a test that never builds a driver. Both
are unmissable the first time two real pieces are put together.

*Rule: for any seam with more than one part, one test must assemble all of them against something
real — a database, not a fake — even when every part has its own test. The unit tests answer "does
this piece work"; only the assembled one answers "is this a system".*

Two smaller lessons from the same review, both about declarations:

- **A field with no reader is a lie with a schema.** `required_roles` on the sink manifest was
  documented as an access control and read by nothing. I had even cited the ADR
  (`D-2026-08-07`) about the *exact* failure — an entitlement defaulting to `[]` — while writing a
  version that defaulted to `[]` and had no `_entitled()` at all. Citing a lesson is not applying it.
- **Prose describing a capability reads as a claim that it exists.** A docstring said Snowflake and
  Oracle "spell it `MERGE`" beside an emitter that only writes `ON CONFLICT`. Nobody lied; the
  sentence was about SQL dialects in general and read as being about this module. When a docstring
  names a thing the code does not do, say which half is true.
## 2026-08-26 — the fixture held constant the axis the function branches on

A third review pass over the same merged work found two more defects, and both are the same shape as
the three before them.

`Condensation.degraded` was overloaded with two facts — "its prose could not be read" (has a row)
and "it resolved to nothing" (has no row) — and the rendered payload then told the model that a
reference nobody could resolve had "recorded figures above", and that a comparison of two protocols
covered the three it was handed. `render_table` placed cells verbatim, so an `observations` value
extracted from a share document, carrying a `|` and a newline, rendered a `rxn-FORGED | 99 | 99 |
best result on file` row that the object does not contain.

Neither was caught, and the reason is one reason. Every fixture in this work is homogeneous on the
axis its function branches on:

- all conditions present, or all absent — never a mix (the fabricated `solvent — → 2-MeTHF`)
- every extraction succeeds, or one fails in isolation (the phantom swaps)
- every protocol under the limit, or one over (the oversize path)
- every reference resolves, or none does — **never the mix** (this pass)
- every cell first-party — **never one that tries to be structure** (this pass)

Five defects, five held-constant axes. The lesson written after each one was about *that* defect;
the family kept shipping because the family was never named.

*Rule for myself: before writing a fixture, list the branches the function under test takes, and
build the collection so its members differ on every one of them. When the function renders text
someone else wrote into a structured format, one member's content must try to be structure. A
fixture where every member takes the same branch proves the branch works, and nothing else — and
that is what "tested" has meant in this whole body of work.*

The corollary, from the same pass: `Field(exclude=True)`, a budget in the wrong currency, and a
renderer that "only places" cells are all the same mistake as a homogeneous fixture — an assumption
about a mechanism, never crossed with the case that would disprove it. The check is cheap and I keep
not running it: **construct the input that would break the belief, and look at the output.**


## 2026-08-25 — A companion-repo change that cannot be pushed is not a deliverable

**What happened.** The GFN multi-step work spanned two repositories by design: the primitives
belong on `Chemclaw3-mcp` under `D-2026-08-16-the-physics-leaves-the-cache-stays`, the composition
belongs here. I built and verified both halves, then discovered at push time that the session's
GitHub scope covered only this repository and `add_repo` with push access needed an approval that
never came. `main` now declares eight tools that no running server answers.

**The rule for next time: check write access to every repository a task spans, before writing code
in any of them.** One `git push --dry-run` at the start would have cost seconds and changed the
plan — the enumerations could have been argued into this tree, or the templates held back until the
companion PR existed. Discovering it after the work is done leaves only bad options.

**A second, smaller one from the same session: `git push --delete` is 403 through the agent proxy**
even where `git push` succeeds. Do not claim a branch was deleted without reading the push output;
`mcp__github__list_branches` is what confirms it.

**And a third: verify mergeability early, not at merge time.** `main` moved three times during this
task's CI runs, each lap costing ~16 minutes, because I only fetched when the merge API refused.
Fetching `origin/main` before opening the PR — and again before each long wait — turns a race into
one rebase.

## 2026-08-26 — a backlog row is a hypothesis, and two of seven were wrong

Working seven queued rows in one pass, two of them turned out to specify the wrong change, and both
failures were the same shape: **a rule stated correctly about one kind of value, then generalized to
a kind it does not fit.**

- `BACKLOG.md` asked for the "compare a field only when both sides recorded it" rule over the two
  setpoints *and* the species sets. It fits a setpoint, where `None` means nobody wrote the number
  down. It does not fit a species set, which is derived from a components list that is present
  either way — so an empty `reagent` set is the record saying *this run used no reagent*, and
  suppressing it erases the most common real change a run-to-run series carries. An existing test
  said so within a minute of applying it.
- The credentials row named three fields. The three were the ones somebody had grepped for; the
  class is seven, and the two the row omitted were the interesting ones — `llm_fallback_api_key`,
  which no redaction list contained *at all*, and `framing_envelope_secret`, which is not a
  credential to anything and is the key an injected envelope would be forged with.

The file's header already says a row is a claim about the code and claims go stale. What this pass
adds is that a row can be *fresh and still wrong*: it is one person's design sketch, and the tree is
what decides. Both corrections cost minutes because a test failed immediately; the cost of not
noticing would have been a merged change that erases data and a redaction that reports success while
matching asterisks.

*Rule for myself: before implementing a queued row, restate its rule in my own words and name the
kinds of value it will apply to. If any two of them differ in what "absent" means, the row is
covering two rules and I am about to ship one of them wrongly. Then run the existing tests for the
function before writing new ones — the test that disagrees with the row is the cheapest review
there is.*

**The corollary, from row 7 and worth more than the row was.** Hardening `llm_api_key` to
`SecretStr` would have silently disabled the log redaction for every credential, because both
readers in `core/logging.py` test `isinstance(value, str)` and a `SecretStr` is not one — and
`str(SecretStr("k"))` is `"**********"`, so the filter would have gone on matching asterisks against
log lines and reporting success. Two protections that look like one, where the stronger-looking one
turns the other off. **When strengthening a type, grep for every `isinstance` on the old one before
touching anything** — the places that check a type are exactly the places that will stop seeing the
value.

**Adding an agent-callable tool touches six declarations, and none of them is in the code you
wrote.** A rotational-profile job passed its own tests, `mypy`, `ruff` and four validators, and the
full suite then failed **eight** ways — every one a guard on a declaration rather than on behaviour:
`.env.example` mirrors every setting, `test_context_floor` caps the static prompt prefix *and*
refuses a single tool over 900 tokens, `test_probe_coverage` wants an eval probe per agent-callable
tool, `test_solvents` pins the count of solvent-taking jobs, `test_templates` pins which templates
cannot be argument-checked, `test_docstring_paths` resolves every backticked path, and a profile's
`tool_names` is an **allow-list** — so the job was reachable and unusable until it was named there.
Two of those were load-bearing: the tool arrived at 1,499 tokens because pydantic publishes a
model's docstring as its JSON-schema `description`, so a nested pair of well-documented specs is
bound to the model on every turn; and the allow-list would have shipped a capability nothing could
call.

*Rule for myself: the moment a new tool, job or template exists, run the declaration guards by name
— `pytest tests/test_context_floor.py tests/test_probe_coverage.py tests/test_solvents.py
tests/test_templates.py tests/test_config.py tests/test_docstring_paths.py` — and grep
`data/profiles/` for the allow-list. Do not wait for the ten-minute sweep to find them, and never
call a change verified on a targeted run: in this tree the interesting tests are the ones guarding
what a change **declares**, not what it computes.*

---

## 2026-08-26 — Deleting a capability: what to keep, and the guard that goes dark

Removing the HPC/DFT tier (`D-2026-08-26-semiempirical-is-the-whole-tier`) was mostly mechanical.
The three parts that were not are the parts worth a rule.

**A guard named after the thing it guards disappears with it, silently.** The `qm` bundle's
"a bundle cannot publish a note itself" test read `assert not hasattr(qm_knowledge,
"write_knowledge_node")`. Delete the module and the assertion becomes vacuously true — no failure,
no signal, and a control that still *reads* like one in review. This is exactly the
`map_to_hpc_identity` shape CLAUDE.md already records, reached from the other direction.

> **Rule.** When deleting a module, grep the suite for tests that name it and ask of each: *would
> this still fail if the invariant were violated by something else?* If not, rewrite it over the
> whole set (an AST walk, a registry scan) before deleting — never delete it with the module, and
> never leave it asserting an absence that nothing can restore.

**An invariant usually outlives the thing it was written against.** The parent-ceiling validator was
phrased against the DFT poll's 24 h budget. The poll is gone; the *rule* — a workflow's execution
timeout must exceed the longest activity under it, or the retry budget is unreachable and the error
names neither setting — applies verbatim to the CREST search that is longest now.

> **Rule.** Before deleting a validator with its subject, restate the rule without naming the
> subject. If it still says something true, it is not the subject's validator — rewrite it. The same
> question re-derived a *default*: `connector_job_timeout_seconds` was 90,000 s because of the DFT
> poll, and every other job had silently inherited a ceiling sized for a tier that never ran.

**A cache outlives the code that filled it.** `calculation_results` is never pruned, so the `dft`
projector had to stay even though nothing can write a `dft` row again — while the `QMJobResult`
entry beside it, keyed by a model name, had to go. "Delete the calculator, keep the reader" is the
rule the module already stated; I nearly deleted both because they sit four lines apart.

**And: run the validators, not just the gate.** `make lint type test` was green with five live false
claims still in the tree — two skills declaring a deleted tool in frontmatter, three backticked
paths naming deleted files. `make prose-validate` and `make skill-validate` found all five. A
capability removal touches *declarations* more than it touches code, and the declaration checkers
are a different make target.

**I declared an environment limitation without checking it, and it was false.** I shipped the
rotational profile saying no barrier had been computed against real xTB and that closing that
"needs the live lane" — a cluster. `tblite` *is* the GFN2 Hamiltonian, ships as a PyPI wheel, and
was **already installed** in the sibling repo's venv; only the `xtb`/`crest` binaries are conda-only,
which the calc server's own `pyproject.toml` says costs speed and the conformer search rather than
the physics. One `import tblite` would have settled it. Running it took twenty minutes and found
two real defects, both on the flagship case: a torsion with one well per period reported **no
barrier at all**, and the discontinuity warning fired on exactly the hindered rotations the feature
exists for. This is the same failure as `CLAUDE.md`'s own "the sandbox is not offline" note, one
level out: I inherited a belief about what the environment could not do from prose rather than from
a probe.

*Rule for myself: an "it needs X" in my own summary is a claim about the environment, and claims
about the environment are cheap to test — check the import, the binary, the port, before writing it
down. And a synthetic fixture can only express failures of the shape it was built in: the fake here
had three wells because n-butane has three, so the one-well case — an amide, the whole point of the
capability — was untestable by construction and passed. When a fake is shaped after one real case,
name the shapes it cannot produce and go find a real instance of each.*

## 2026-08-26 — a grep that lists a file is not a grep that read it

Tightening `_check_classification` to refuse an empty `tools` list broke five
tests in `tests/test_langgraph_connectors.py`, and CI found them rather than I
did. The file *was* in my first grep's output. I looked at the hit
(`tools=list(allowed)`), saw a variable being passed through, and moved on
without asking what `allowed` defaults to — which was `()`, the exact value the
change makes illegal.

Two more of the same shape landed earlier in the pass (`test_capability_degradation`,
`test_hot_path_caching`), and I only learned about those because a reviewer
working a different lens mentioned "10 pre-existing HttpEndpoint-validation
failures" in passing.

**The rule for myself: when a change makes a previously-legal value illegal,
the search is for every site that can *produce* that value, not for every site
that names the type.** Grep the constructor, then read each hit to the point
where the argument's value is decided — a default, a fixture, a parametrisation
— rather than to the point where it is passed along. And when the changed thing
is a validation rule, run the whole suite before pushing rather than the suites
that obviously relate to it: four of the five files that broke had nothing to do
with connectors.

## 2026-08-27 — a bound's name is prose, and my own guard proved it

**The pattern.** Four defects fixed in the BO layer this session were one shape: *a quantity that is
checked and a quantity that is spent, differing by a factor nobody multiplied.* `bo_max_rounds`
bounded rounds while `batch` made a round cost N evaluations. The exhaustion test counted feasible
cells on one side and every distinct run on the other. The progress report divided those same two
mismatched counts and could print "7 out of 6". Each one reads correctly at the call site; each one
is wrong by a factor that lives somewhere else.

**The correction I had to apply to myself.** The guard I wrote to bound screening-design size — the
fix for one of those four — shipped in its first version with the same defect *inside it*: it
stopped multiplying once the running product passed the ceiling, and a partial product shifted right
by `n_generators` lands back under the ceiling. Measured: 40 two-level factors at one generator
passed a 4 096 ceiling on a partial product of 8 192, against a true design of 2^39 rows. I did not
catch it by re-reading the code. I caught it by writing the arithmetic into a script and running it.

**The rule for next time.** When a fix introduces a bound, compute the bounded quantity out loud —
in a script, with a real adversarial input — before believing the guard. Reading a bound tells you
what its author meant; running it tells you what it does. This repo already says that about prose
and docstrings. A bound's *name* is prose too: `bo_max_rounds` reads as a cost ceiling and is a loop
counter, and the gap between those two readings survived for as long as batching existed.

**Second rule, from the same session.** Three of my six audit findings were partly or wholly wrong
when checked against the code — a "missing" `TOOL_METHOD` entry that was present, a "never fixed"
MOBO gap that was a deliberate argued refusal, a retention premise that understated the problem
tenfold. A research pass produces hypotheses, not findings. Verify each against the source before
fixing it, and be willing to report "this one was wrong" — the subagents that did exactly that
produced the most useful work of the session.

## 2026-08-27 — run the gate, not a subset of it (twice in one branch)

Two CI/full-suite failures on this branch, same shape both times: **I verified with a narrower
command than the one that decides.**

1. `mypy --strict src/` was clean; `make type` runs `mypy src examples tests` and found a
   `tuple[int, bool] == int` left over from a signature change, *in a test file*.
2. A 486-test scoped run was clean; the whole-repo run found three new settings undocumented in
   `.env.example`, which no BO test could have seen.

Both were caught downstream, both were trivial to fix, and both were avoidable by typing eight
more characters. The rule: **before pushing, run the repo's own gate target verbatim** — here
`make lint type test` — rather than the scoped equivalent I reached for while iterating. A scoped
run is the right tool *while* iterating and the wrong evidence for "this is done". CLAUDE.md
already says a step is done only when `make lint type test` is green; the failure was reading that
as a description of CI rather than as an instruction to me.

## 2026-08-28 — a guess about which test failed is not a diagnosis

Mid-run, the streaming pytest output showed one `F` at the 16% mark. Rather than wait for the
summary — twelve minutes off, and it names the test — I mapped 16% onto `pytest --collect-only`'s
ordering, landed on `test_connector_transport.py`, noticed it holds wall-clock tests, and ran that
file alone. It passed, and I recorded the failure as a timing flake under load. Both halves of that
were wrong: the file was never the failing one, and its passing was evidence about nothing.

The actual failure was `test_config.py::test_env_example_documents_every_field` — a new
`service_max_plan_scans` setting that I had not documented in `.env.example`. A real gap in my own
change, one line to fix, and I had classified it away as somebody else's flakiness.

The mechanism is worth naming, because "I was impatient" is not it. **A percentage in a progress
bar is a position in an ordering I reconstructed, not an identifier.** Two of the steps between it
and a test name are my own inference, and running the guessed file can only ever *fail* to falsify
the guess — a pass is consistent with "not this file" as well as with "flaky here". I built a test
whose green result told me nothing and then read it as confirmation.

The rule: **when a run reports a failure, get the failure's name from the run.** If waiting is not
acceptable, re-run the suspect *with the same seed and load*, or grep the partial log — do not
index a progress percentage into a collection listing. And never let "flake" be the conclusion of a
chain that starts with a guess about identity; it is the one classification that requires knowing
exactly which test, since the whole claim is about that test's history.

## 2026-08-28 — a subagent's baseline claim is a claim, and the baseline is cheap

Four parallel audits produced unusually good work on this branch — three of the four defects they
found were real, verified against a live database, and are now closed with tests that go red
without the fix. One claim was not: an agent reported a named test as "failing on a clean
baseline too — pre-existing, unrelated". I had started `make lint type test` on the untouched tree
before any of them reported, and it came back **5,444 passed, 11 skipped, exit 0**.

Had I not had that run in hand, the cheap move would have been to believe it — it is exactly the
shape of a true statement, it names a real test, and accepting it costs nothing in the moment. What
it would have cost later is a whole class of failure classified away in advance: every subsequent
red in that file would have read as "the known pre-existing one".

The rule is not "distrust subagents" — this session's evidence runs the other way. It is:
**take the baseline before the first edit, always, and let it be the thing that adjudicates any
claim about what was already broken.** It costs one backgrounded command started at the moment the
work begins, and it is the only artifact that can tell "my change broke this" from "this was
already broken" without argument. Related to the 2026-08-27 lesson about running the gate rather
than a subset of it: same command, one run earlier.

## 2026-08-28 — the tests I wrote alongside a change cannot find the defects in it

The memory-bounds change merged green: `make lint type test` with Postgres up, 5,532 passed, eight
new tests, three of them proven red against the pre-fix code. An adversarial review then found
**three real defects in it**, including one that made erasure impossible for a whole deployment —
`jsonb_array_elements` raises on a JSON `null`, and one such row in a table the sweep does not even
erase aborted every actor's erasure, permanently.

Every one of my tests passed with that defect present. Not because they were careless: because I
wrote them from the same understanding that produced the defect. I tested the payload shape I had
in mind (`{"publications": [...]}`) and never asked what the other shapes of a `JSONB NOT NULL`
column with no CHECK would do. Same shape for the blob anti-join: I tested "another person's
session spares the blob" and never "an orphan session spares it", which is the case that keeps the
*leaver's own* data alive.

The rule: **when a change adds a predicate, enumerate the inputs that predicate cannot read, and
parametrize over them.** Not "add an edge case" — enumerate the domain and cover the part outside
what the happy path constructs. For SQL over a payload, that means every `jsonb_typeof`. For an
anti-join, it means every reason the join might or might not find a row, including the rows nothing
owns.

And the second-order rule, which is the one that actually caught these: **review your own merged
work adversarially, against a live system, with someone else's eyes.** Four parallel passes cost
one message and found what one careful author plus a green gate did not. The gate proves the change
did not break what was already tested; it says nothing about what the change itself introduced.

## 2026-08-28 — `git stash -u` is `git add -A` wearing a different verb

I ran `git stash -u` to check whether a suite failure was pre-existing, **while three subagents were
writing files in the tree**. It worked — the pop restored everything and the stash list came back
empty — and that is luck rather than evidence. Rule 4 says the working tree is not mine when other
agents are in it and names `git add -A` as the weapon; `stash -u` is strictly worse, because it
*removes* their untracked files for the duration and a write landing in that window has nowhere to
go. Two hundred tests were untracked at the time.

The check I wanted was cheap and safe: run the failing test alone against the current tree first,
and only reach for a clean baseline once the tree is mine. As it turned out I did not need a
baseline at all — the failure was the docker daemon dying mid-run, which rule 3 already predicts and
which `docker info` answers in one call.

**Before any command that rewrites the working tree wholesale — `stash`, `clean`, `checkout .`,
`reset --hard` — run `ListAgents`.** If anything is running, the command is not available to me.

## 2026-08-28 — a count in prose went stale inside one hour

I wrote "thirteen deterministic verdicts" in a package README, a module docstring and an ADR. Two
hours later I added a fourteenth check and all three were false. `D-2026-08-01-the-count-lives-in-
the-test-not-in-the-prose` is about exactly this and I had read it that morning.

The tell is that I *derived* the number by counting a tuple I had just written, which is the same
act as hardcoding it. Three of the four numbers I have written this session went stale or were wrong
(the fourth, a token measurement, only survived because I re-ran it). A number in prose is a claim
that needs a producer; where there is none, name the producer instead — "`check_ids()` is the list"
costs the same characters and cannot rot.

**Corollary that bit separately:** I then described severities *per check* ("charge_is_consistent is
a warning") when the function returns `blocker` on three branches and `warning` on two. A property
that varies by branch cannot be summarised by listing function names, and deriving it from one
empty-design call — which is what I did — samples exactly one branch.

## 2026-08-28 — a clean rebuild is not a neutral act when a harness serves the build

I ran `rm -rf dist && npm run build` to check a security gate honestly, and eight Playwright tests
went red. Nothing was broken: the e2e `webServer` serves `dist/server.js` and `dist/client`, and that
suite runs unauthenticated, so it needs the `ALLOW_DEV_AUTH=true` build that CI makes for it — my
production build had correctly stripped the dev auth provider out.

I nearly filed it as a UI regression. What stopped it was reading the browser console line in the
Playwright output instead of the assertion: `AUTH_MODE=dev is not permitted in this production
build`, which names the cause exactly. **Rule 12 again — read the failure before classifying it —
and one specific to build artefacts: when a test harness consumes a build directory, the build flags
are part of the fixture.** Rebuild the way the harness does, and check `.github/workflows/` for
which flags that is rather than guessing.

## 2026-08-29 — a test written by the author inherits the author's belief

Seven reviewers with fresh context read a change I had just shipped green — `make lint type test`
passing with Postgres and Temporal up, every declaration gate held, and six defects I had already
found while building it. They found about thirty more, and I reproduced every one before acting.

The pattern in almost all of them: **a docstring stating the correct control, beside code that does
not implement it.** Not a control I forgot — one I described. The MCP face documented its read-only
export surface and served *zero* tools in production. The pending route says "'I asked the QA lead
to approve this' must not mean 'and I may approve it myself'" and the only producer of approvals
left the routing unset, so the requester could self-approve. The evidence tool claimed an ownership
check that lives on a FastAPI route it never touches. The digest's comment says delivery runs after
the acknowledgement; it ran before.

The rule I need, because it is the mechanism rather than the symptom:

**When I write a test for code I just wrote, it inherits what I believed the code does — so it
proves the mechanism and never the instance.**

Three of my own tests demonstrate it exactly:

- `test_mcp_face.py` imports `chemclaw_agent` to populate the tool registry. That import is what
  production lacked. The test could not fail.
- `test_operations.py` asserts no caller free text escapes a reading, and writes its marker into
  four columns the reading never selects — while seeding the one column that *is* attacker-
  influenceable with a safe literal.
- `test_units.py` proves case separates molarity from length using `M`/`m` and `mM`/`mm`. Those two
  rungs were right. `nM` resolved to nanometre and `µm` to micromolar.

So, concretely, for the next change:

1. **Test the production entrypoint, not a convenient import of it.** If a module is populated by
   import side effect, assert it in a subprocess that imports only what the deployment imports.
2. **Seed the adversarial value into the field the code actually reads**, and check the read path
   selects that field. A marker in a column nobody selects proves nothing.
3. **When a mechanism has a series (a prefix ladder, a state vocabulary, a status set), test the
   whole series or state which rungs are untested.** Two examples prove the mechanism exists, not
   that the table is right.
4. **When a docstring names a control, go and find the code that enforces it before believing the
   docstring** — including, especially, my own from an hour ago.

And the cheap fix that found all of it: fan reviewers out over a diff with **fresh context**, scoped
by failure domain. A reviewer who shares my belief about the code cannot see any of these; one who
has only the code can.

## 2026-08-29 — a red check is a claim about the system *or* about the check, and the odds are even

A four-repo e2e campaign produced six findings. **Three of them were the check being wrong, not the
code.** That ratio is the lesson: walking in, I treated every red as a defect report, and on this
tree that assumption is close to a coin flip.

- `prose yields its numbers` failed **0/12** and read exactly like a broken extraction. It was
  asserting the opposite of `D-2026-08-26-a-transcription-may-not-infer-a-setpoint`, so it could
  only ever fail. Its stated premise — "the condition is simply gone" — was false, and one
  measurement showed the value sitting on step 2 with its sentence verbatim. Had I "fixed" the
  adapter to satisfy the check, I would have made the system violate a merged ADR.
- `f-malformed-json` sent a *truncated* argument document and demanded it be reported. LangChain
  repairs truncation through `parse_partial_json` before anything first-party sees it — a fact the
  module under test already documents, having once corrected its own docstring for the same
  confusion. Unsatisfiable by construction, and it left the genuinely reachable case untested.
- Four of the storm's failures were my own missing `CHEMCLAW_MCP_REPO`, which made its chaos
  primitive kill a process it then could not restart. The damage surfaced two families later as
  unrelated red checks, and was invisible from every one of them.

**The rule: before fixing what a red check points at, ask what the check asserts and whether the
system is documented to do that.** Read the module's own docstring and grep `docs/decisions/` for
the behaviour before touching code. A check that has never passed is evidence about the check.

**And the corollary, which cost the most here: re-run once with the environment fully set before
believing any failure.** A lane failure and a real failure look identical in a report — both are a
red row with a plausible observation. Only the re-run separates them, and on this campaign it
changed the verdict on three of four storm failures. The re-run is cheap; a fix aimed at the wrong
target is not.

Related, from the same run: my *own* fix's first version was wrong in a way my own two new tests
could not see, because I wrote them from the same understanding of the layout that produced the
off-by-one. `parents[3]` raised a bare `IndexError` on the shipped default. That is the 2026-08-28
lesson repeating one day later, so the enumeration habit is not yet automatic: **when a change adds
an index or a predicate, write down the inputs it cannot read before writing the test.**

## 2026-08-29 — a handed-over measurement is a claim, not a fact

The 2026-08-28 campaign handed over F6 with two symptoms and a named next step. One symptom was
false and the next step pointed at the wrong seam:

- `chemclaw_invalid_tool_calls_total` was reported as carrying **no samples**. It carries two per
  turn. The same lane, the same behaviour, the same probe — `curl /metrics | grep` says
  `chemclaw_invalid_tool_calls_total{tool="find_notes"} 2`.
- The step named was "enter LangChain's streaming tool-call assembly", flagged as this system's
  most defect-prone seam. That seam is sound at every step, and each step was measurable in
  minutes: the aggregated chunk carries `invalid_tool_calls`, `message_chunk_to_message` preserves
  it, and the middleware receives it. Roughly an hour of work sat behind a warning about a seam
  that turned out to be innocent.

The real cause was one layer away and had nothing to do with streaming: `tool_failed` is raised by
a `wrap_tool_call` middleware, and a call whose arguments never parsed never enters the tool chain.

**The rule: re-run the handed-over measurement first, before reading the diagnosis attached to
it.** A previous session's finding has exactly the standing of a docstring — evidence about what
its author believed. This repository already says prose is never evidence about what the code does;
a measurement written into prose becomes prose the moment the run ends. Reproducing this one cost
two commands against a lane that was going up anyway, and it inverted both halves of the handover.

**And the corollary about scary labels:** "this system's most defect-prone seam" is a reason to
measure that seam *first and cheaply*, not a reason to treat entering it as the cost of the task.
Four print statements settled it.

## 2026-08-29 — a precedent on disk is not the rule that governs it

Merging `origin/main` into the eight-findings branch put two files on `073`: `main`'s merged
`073_experiment_protocols.sql` and this branch's unmerged `073_pending_requests.sql`. I checked
whether that mattered by reading `core/migrate.py` — it keys the ledger on the *full filename* and
applies in sorted filename order — then noted that `main` already carries `037_*` and `043_*` pairs,
and told the user it was "not a problem". The full suite disagreed:
`test_no_two_migrations_claim_one_number` failed.

Both halves of my reasoning were true and the conclusion was still wrong. The runner genuinely does
not care. The four existing files are **grandfathered by name** in an exemption list whose docstring
says exactly what it is for: *"the check exists for the next one — caught at review, when a rename is
still free. Adding a fifth name to it is a visible act in a diff."* Reasoning from the precedent led
me toward the one move the rule was written to make expensive.

**The rule: when the tree shows a pattern that looks permitted, find the gate that governs it before
concluding it is.** Two files that look like a merged pair are evidence about history, not about
policy. Here the policy was one `grep -rn "claim_one_number" tests/` away, and my earlier grep for
`duplicat|uniq` missed it purely because the test is named in the repository's own prose style rather
than in the vocabulary I guessed at.

The corollary that would have caught it faster: **grep the test suite for the concept, not for the
word I would have used.** This repository names its checks as sentences (`test_a_trail_nobody_can_
read...`, `test_no_two_migrations_claim_one_number`), so a keyword search over identifiers is a
weaker instrument here than in most trees — search the *subject* (`migration`, `number`) and read the
handful of hits.

## 2026-08-29 — four of my five worst defects were a check that could not fail

An adversarial review of a tier I had just merged found **fifteen** defects under a green 231-test
suite. Four of the five worst were the same shape: a **blocker that could not fail**, each sitting
under a passing test I had written from the same misunderstanding as the code.

- `components_resolve` tested `canonical == smiles and not _parses(smiles)`. I believed
  `canonical_smiles` returns its input unchanged on a bad input. It does not — RDKit stops at
  whitespace, so `"CCO junk"` canonicalises *successfully* to `"CCO"`, a smaller molecule. The first
  clause is false for exactly the class the check exists for, so the second was never reached and the
  detail read `1 structures parse` about a structure that does not.
- `forbidden_absent` compared `canonical_smiles("DMF")` — the literal string `"DMF"` — against a set
  of canonical SMILES. The comment beside it explained at length why looking at the structure
  matters. It could never match.
- `charge_is_consistent` put `reference.amount_mmol > 0` inside the comprehension that finds
  disagreements, so a limiting reagent at `0.0` emptied the list and passed.
- `layout_fits` never compared `rows`/`columns` to `plate_format`.

**What each has in common is that my test asserted the same wrong model of the world as the code.**
`test_components_resolve_blocks_a_structure_rdkit_cannot_read` passes `"not-a-smiles"` — which
RDKit rejects outright, the one case the conjunction handles. I never tried `"CCO junk"`, because
the code and the test came out of one belief.

The rules I am taking:

1. **For a check, enumerate the input *classes* before writing either the code or the test**, and
   name the one the check exists for. Here it was "a string RDKit accepts as a different, smaller
   molecule" — the class `require_molecule`'s own docstring is entirely about, in a function I had
   read that morning. A both-directions test is not enough when both directions come from one idea.
2. **A test I write beside my own code is a consistency check, not a search.** The searching has to
   be done by something that did not write the code. This one cost 15 findings; commissioning it
   was the highest-value hour of the task and it should not have been the last hour.
3. **When a comment argues for a behaviour, run the behaviour.** Three of the four announced
   themselves in their own prose — `forbidden_absent`'s comment describes the DMF/`CN(C)C=O` case as
   the reason for code that cannot do it. I wrote the comment and the code in one pass and checked
   neither against the other. The existing rule 14 says the docstring is the best bug detector;
   the corollary is that it is useless on prose *I* wrote in the same breath as the code.

## 2026-08-29 — I verified `make test` and CI runs `make cov`

I ran `make lint type test` five times across a change, called it green, and CI failed — on a test
`make test` runs too, but in a job I had never reproduced. The `ci` target is
`lint type cov kg-validate … deps-audit`, and **`cov` is not `test`**: it is
`pytest --cov --cov-report=term-missing`, which adds a coverage floor and, more to the point here,
changes the timing profile of every performance-budget test in the suite. The failure was a
per-turn graph-compile budget at 408 ms against a 400 ms bound.

Rule 5 already says "run the gate's own command, at the gate's own scope, unpiped", and I read the
`Makefile`'s `cov` line the day I started. What I did not do was ask **which target CI actually
invokes** — I inferred it from `CLAUDE.md`'s prose ("the gate: `make lint` · `make type` ·
`make test`"), which describes the developer loop rather than the CI job. The authority is
`.github/workflows/`, and it costs one `grep`.

**Before claiming a change is green: `grep -n 'make ' .github/workflows/*.yml` and run exactly
those.** For this repository that is `make ci`, and the two targets it has that the prose does not
are `cov` and `deps-audit`.

The finding underneath was worth having, so the cost was not wasted: four new tools raised the
per-turn compile 30 ms (209 → 239, isolated by deleting their registration import), and profiling
put **79% of the whole build** in `langchain_core.tools.convert` re-deriving a pydantic model from
every tool's signature on every build. Build time is proportional to schema size exactly as prompt
cost is — a second cost of an oversized tool schema that nobody here had measured.

## 2026-08-29 — a fix can be paid for with a record that does not exist

The second adversarial pass over `chemclaw.protocols` found six more defects on top of the first
pass's fifteen. The one worth a rule is not any of the six; it is the shape the largest one had.

`store.advanced()` demotes an `approved` design back to `draft` when a revision lands on it. That is
correct, and the docstring arguing for it ends: *"Which revision was approved stays recoverable:
`set_status` records it."* `set_status` wrote one column on the header row and logged a line that
did not carry the revision. **I wrote the demotion and the sentence excusing its cost in the same
commit, one screen apart, and the sentence was an invention.**

Rule 14 and the previous entry's rule 3 both say a docstring is a bug detector and that prose I
wrote in the same breath as the code is not. This is the sharper case, because the false sentence
was not describing the code beside it — it was describing *somewhere else*, and describing it as a
reason to accept a loss. So:

1. **A cost-is-affordable claim names a mechanism; open the mechanism.** "X stays recoverable
   because Y records it" is a claim about Y, in a comment about X. It reads as reassurance and it
   is a testable assertion about a different function. Every time I justify removing, demoting or
   overwriting something by naming what still holds it, the next action is to open that thing.
2. **A field the API accepts and never reads is a control that lies.** `POST
   /protocols/{id}/status` validated a `reason` to 2,000 characters and dropped it — while the UI
   labelled the box "recorded with the move", *disabled every button until it was filled in*, and
   confirmed "recorded against you with the reason you wrote". That is worse than a dead parameter:
   it is a promise made to a person, three times, in their own screen. **Trace every request field
   to a write, and grep the consumer repository for the ones a person types.**
3. **A fixture that agrees with my type proves nothing about the service.** The UI's
   `GET /protocols/{id}` type was nested where the service is flat, so `revision.design` was
   `undefined` and the document page threw on its first field — under 808 green unit tests and 8
   green browser tests, one of whose docstrings says it exists to prove the page "renders against a
   real proxied response rather than a stubbed one". Every stub was written from the same belief as
   the type. **A cross-repository shape is verified by dumping the producer's own schema, not by
   re-reading the consumer.** One `model_json_schema()` call ended a question three test suites
   could not.
4. **Resolve a merge, then grep for the markers.** `docs/decisions/README.md` went to a commit with
   `<<<<<<< HEAD` in it and eight of `origin/main`'s ADR rows dropped. `make lint` and `make type`
   were both green, because the file is Markdown. The suite caught it; my own reading did not.

5. **Do not touch the shared database while a full suite is running against it.** I started
   `make cov`, then ran a mutation that dropped a table and re-migrated it. The suite's own
   Postgres-backed tests were reading that table at the time, so the run stopped being evidence
   about anything and had to be thrown away and restarted. The sandbox has one Postgres and
   `tests/pg.py` points every backend test at it. **While a suite is running, the only safe
   commands are read-only ones** — a mutation experiment waits, or runs against a database of its
   own.

6. **The "run exactly what CI runs" rule is per repository, and the UI's gate is seven commands.**
   I ran `typecheck`, `lint` and `test` in `Chemclaw3_ui`, called it green, and CI failed on
   `npm run format:check`. The check job is `npm audit --omit=dev --audit-level=high`, `typecheck`,
   `lint`, `format:check`, `test`, `check:contrast`, `build`; the e2e job adds **both directions**
   of `check:no-dev-auth` (a default build must not carry the dev provider, an `ALLOW_DEV_AUTH=true`
   build must) and `test:e2e`. Same `grep -n 'run: ' .github/workflows/*.yml`, different repository.

## 2026-08-29 (second entry) — I shipped, then a review found four defects in what I shipped

PR #282 merged green: `make lint type test`, a live storm family going 7/8 → 8/8, and a live
measurement of the exact behaviour. Four adversarial reviewers over the merged commit then found
four real problems: one defect introduced by the change, one pre-existing field it made
reachable, one process failure, and one false claim in its own ADR. "Three introduced" is what I
first wrote, and it is not what the four bullets below say.

What the green gate could not see, and why:

- **The new event fired on turns that succeeded.** Every test I wrote asked "is the lost call
  announced?" and none asked "is anything announced when nothing was lost?" — so `evals/live`
  scoring a clean turn `failed_loudly=True`, `turn_costs` booking failures against successful
  calls, and `Chemclaw3_ui` painting two red rows above a good answer were all invisible to a suite
  that only tested the failure case.
- **I never enumerated the consumers.** `ToolFailedEvent` had exactly one producer before this;
  adding a second changed the meaning of every reader of it, and I read none of them. Two of the
  three broken readers are named in the event's own docstring.
- **My ADR asserted a mitigation I had not checked.** I wrote that `reason` distinguished a
  repaired step from a broken tool. `reason` names five gates and both cases carry `None` — one
  grep would have shown it. That is the same class as the handover measurement I had corrected in
  this very session, committed by me, in the document recording the correction.
- **A field documented as bounded was not.** `invalid_tool_calls`' docstring said every field was
  bounded on the way out; I read the docstring instead of the constructor three lines below it.
  (An earlier version of this line attributed that claim to `BrokenCall.error`'s own docstring,
  which says no such thing — a correction about misreading prose, itself misreading prose.)

**The rule: when a change adds a second producer of an existing event, record, or metric, list
every consumer of it and say what each now sees.** Not "does my new case work" — "what does the
*old* reader make of my new case". The cheap version is one grep for the type name across `src/`
and the sibling repos, then one sentence per hit.

**And the corollary: a test suite that only exercises the failure path proves nothing about the
success path.** For any change that fires on a condition, write the test where the condition is
absent first. `test_a_repair_that_works_announces_nothing_because_nothing_was_lost` is the test
that should have existed before the feature did.


## 2026-08-29 (third entry) — the third pass, and a self-inflicted wound in the middle of it

A fresh four-way review of the two merged commits found seven more things. The pattern across all
three passes is one thing, stated three ways:

- **#282**: I tested the failure path and not the success path, so a new event firing on turns that
  succeeded was invisible.
- **#284**: I fixed the announcement and shipped `_empty_answer_event` with **no test at all** —
  `grep "reason to start from" tests/` returned only the source line — and its one behaviour I did
  think about (failures) folded in the one I did not (refusals), contradicting a merged ADR.
- Both: prose asserting the rule the same commit had just reverted, 200 lines below the paragraph I
  corrected.

**The rule, and it is the same rule each time: the thing I did not think about is the thing with no
test.** Before committing, list every input the new code branches on and name the test for each
branch. `tool_refusals` was a term in an expression I wrote and never once considered.

**And a mistake made while proving the fix.** My mutation script restored with
`git checkout -- src/` between runs. `src/` held all of the session's uncommitted work, so the first
"restore" discarded every source fix I had just made — silently, because the tests live in `tests/`
and only the *baseline* went red. I noticed because the baseline read `8 failed` when it should have
read all-pass, and I nearly read that as a real regression.

**The rule: commit before you mutate.** A revert-to-prove loop must restore from a committed
baseline (`git checkout HEAD -- <one file>`), never from the index of a dirty tree, and never with a
directory-wide path. Check the baseline is green *first*: a mutation run whose baseline is red is
measuring nothing.

**One more, from the same loop:** `str.replace(old, new, 1)` hits the first occurrence, and this
middleware writes its guard twice — once per hook. The mutation landed on the sync path while the
test drove the async one, so the guard read as unpinned when it was pinned. The parametrized test
that came out of that now covers both, which is the coverage the accident found.


## 2026-08-29 (fourth entry) — three numbers I published that were not properties

A fourth review cycle, this time over my own fixes rather than over the code they fixed,
**falsified three measurements I had already written into an ADR.** Every one of them was a real
number when I took it. Every one was then written down as a fact about the system.

- *"A chemist's abandonment was reverted 20 times out of 20."* Re-run five times: 20, 18, 18, 20,
  17 — 93 of 100. The race is overwhelmingly likely, not certain. I ran it once, saw a clean sweep,
  and reported the sweep.
- *"2.61 s at the 4 MB cap payload, down from 46 s."* That payload cannot be constructed any more —
  the same commit added the `max_length` ceilings that refuse it. The largest design now legal
  diffs in ~0.3 s (0.060 s when that was written, on one filling of the free-text
  fields — the byte size at those counts ranges 482 KB to 1382 KB and the diff time does not move
  with it), and at its ceiling the quadratic scan costs 22.4 ms against `Counter`'s 0.107
  ms. **Two fixes shipped together and I gave the credit to the wrong one**, which then went into a
  code comment as the reason that line exists.
- *"90.6 ms → 15.7 ms."* Reproduces at 384 arms, not at 24 (4x there). And the method it described
  was reverted in the same cycle, so the claim outlived its subject.

**The rule: a number going into the record is run more than once, and reported as what varied.**
One run, in one configuration, immediately after the change it is meant to justify, is the most
flattering measurement available — and it is the one I reach for, because at that moment I am
looking for confirmation rather than for the distribution. `CLAUDE.md` says "measure it, don't argue
it" and says nothing about how many times; for a claim that ends up in an ADR or a code comment, the
answer is more than once.

**The corollary, which is the part that generalises past measurement.** When two changes ship in one
commit and the outcome improves, I attribute the improvement to the more interesting one. Here the
interesting change was an algorithm (O(n²) → `Counter`) and the boring one was a refusal
(`max_length`), and the boring one did all the work. Before writing "X fixed Y", disable X alone and
check that Y comes back.

**And the same instrument found four more of the same kind in the UI**, all fixture-shaped: a
transcript index I "corrected" to a value that gave two messages one key, a plate axis where rows
and columns used different rules, an error path written for a `detail` shape and blind to the one it
was written for, and an interface promising a field no read returns. Each was found by reading the
*producer* — `_transcript`'s `enumerate`, `place()`'s label arithmetic, the route's 409 body,
`DesignOut`'s `extra="forbid"`. **When a fixture and the code agree, that is not evidence; the
producer is the evidence.** I now read the producer before writing the fixture, in both repositories.

## 2026-08-30 — the fourth pass, and the signal I kept ignoring

The fourth review of one subject ended in a rewrite: 113 lines of retry machinery replaced by 42
that move a call from one field to another. (I first wrote that as "~180 replaced by ~20" — a 9x
reduction I never measured, against a real 2.7x. See the fifth entry.) What is worth carrying is not the design —
it is that **three rounds of "found a defect, fixed the defect" was itself the finding**, and I read
it three times as three unrelated bugs.

**The rule: when consecutive fixes each introduce the next defect, stop fixing and look at the
design.** The diagnostic is not the count. It is that every fix *rebuilt something the surrounding
system already provides* — a loop ceiling because the loop cap could not see the extra model call,
a reporting ceiling because nothing bounded the corrective message, an announcement rule because no
`tool_failed` could be raised, a call-id guard because the announcement had no id. Four hand-built
substitutes for four things a graph iteration gives away. A mechanism that needs its own version of
what it sits next to is standing outside it.

**A second-order mistake, and the one I am least comfortable with: I propagated two agent-supplied
specifics into the permanent record without reading either source.** `"0 failures / 3 held"` was
quoted as `Chemclaw3_ui`'s header; the component renders `` `${held} refusal${...}` `` and omits the
failure clause entirely at zero, so the string I quoted cannot be produced. `841 kB` was
unreproducible — 623 kB at the commit it described. Both reached four first-party files, an ADR and
the ledger, because a subagent reported them and they sounded like measurements.

**The rule: a number or a quoted string from a subagent is a claim, not a measurement, until I have
opened the file or run the thing.** The cost of checking is one `grep`; the cost of not checking is
a false fact in a record that outlives the session. Quoting a *file I have not read* is the specific
act to refuse — a measurement I can at least re-run.

**Third: a merge resolver that reconciles two append-only registers by key must treat a *changed*
row as a conflict, not a duplicate.** Mine keyed the ADR ledger by id and did `rows[id] = line`
iterating ours-then-theirs, so `main`'s copy won every collision and silently discarded a correction
the branch had made to an existing row. It reported a clean merge. Nothing was lost that a test
could see, which is exactly why it survived to be noticed a week later.

**Fourth, on tests that pass under the defect they were written for.** The parse-error test pinned
its 79-character fixture, which fit inside both the head window and the tail window — so the
head-bounded implementation it was written to reject passed it. A bound is only tested by a fixture
that *exceeds* it, and the honest form is a diff against the alternative implementation rather than
a property both share. `test_the_parse_error_keeps_its_reason_where_head_bounding_would_lose_it`
now asserts `"Expecting property name" not in _bounded_text(raw)` beside the positive case, so the
two cannot silently converge again.

**Fifth: assert the mutation is caught by the test written for it, not merely by the suite.** My
first mutation run used `-x`, so a mutation was reported "CAUGHT" by whichever test ran first. Two
of seven were caught by an older test and one — dropping the model's call id — *survived* the test
whose comment claimed to prove it, because the announcer and the refusal read the same
`request.tool_call["id"]` and pair with each other whatever it is. That comment was a false claim
about a control, written by me, one commit after writing the rule against them. It is corrected in
place and the real proof moved to the producer.


## 2026-08-30 — six strangers found 37 defects in code four of my own reviews had passed

A fifth review cycle over the prescriptive tier, run by six subagents with **fresh context windows**
over disjoint slices. They found 37 defects here and 8 in the frontend, in code that had already
been through four of my own review-fix cycles and a green 233-test suite.

**The pattern in what they found that I did not.** Almost every one is a place where I read my own
*intention* instead of the text:

- I wrote `_structures`' docstring arguing that `reaction_smiles` says "what is being asked for"
  while the set says "what the design *does*" — and left `request.components` in the same set, which
  is the identical mistake one field over. It made the commonest ask in process chemistry ("get me
  out of DMF", which names DMF and forbids it in one sentence) permanently unstorable.
- I wrote `_number`'s docstring naming `1.23457e+06` as the defect it fixed. The fix covered only
  integral values and the docstring's own literal example still reproduced.
- I wrote `_cell` to stop free text restructuring a table, and never asked the same question about
  the text *outside* tables — where a hazard string forged a second `## Waste` section.
- I fixed who supplies the quote for `basis="stated"` and never revisited what `stated` means, so
  the value stayed unattested and four limits a chemist never named were stored as their own words.

**The rule: after fixing a defect, search for its shape elsewhere.** Every one of those is the
fixed bug living one field, one function or one layer along. I fix the instance and write a
docstring about the class, which reads as if I had fixed the class.

**And the second one, which is new to me: a fix can silently retire a test somewhere else.** The
audit applied 21 source mutations that left the suite green. The sharpest:
`test_two_writers_racing_on_one_head_lose_as_a_revision_conflict` exists to prove the primary key
decides a race and asserts, emphatically, that the loser "cannot pass by inheritance" from
`UniqueViolation`. A `FOR UPDATE` added *later*, for an unrelated defect, serialises those writers —
so the branch is now unreachable, replacing the handler with a raised `AssertionError` leaves the
suite green, and the test goes on passing while its docstring is false. Nothing signals this.

The remedy is cheap and I now use it: **for any test whose value is the branch it reaches, break the
source and check it goes red.** I did that for the render, authorization, store and diff tests added
in this cycle, and it caught two of my own tests asserting something other than what I thought.

**The sentence that stood here counted them, and the count was wrong three different ways.** It read
"11 of 13 render tests, all 4 authorization tests, all 4 store tests, both diff tests" — 21 — while
the commit and the merge said "all 23 tests added in this cycle" and the ADR said four. A later
review collected the node ids at both ends of the branch and measured **41 new node ids over 36 new
test functions**: authorization +7 (not 4), checks +8 and design_tools +3 (which this sentence
omitted entirely), render +15, store +4. The store figure was the only one right. **A count of my
own work is a measurement like any other, and I wrote three of them from memory in one cycle.**
Either collect it — `pytest --collect-only -q` at both ends is one command — or describe the
practice without a number, which is what the paragraph above now does.

**A measurement note, continuing the previous entry's rule.** The reviewers handed me numbers and I
re-ran every one before using it. Two did not reproduce as reported: the sign-off race was described
as a concurrency defect and reproduces 0/100 as a race but 100/100 as plain latency, and the torn
read needed `REPEATABLE READ` rather than merely one transaction. Had I taken either on trust I
would have written a fix for the wrong mechanism and a commit message asserting it. **Someone
else's measurement is evidence about their run, not about mine.** The torn-read figure I published
for that second one — "2/25" — is the same lesson turned on me: a later reviewer got 1/25, 3/25 and
1/25 on three runs, because a race rate is a *rate* and I quoted one sample of it as a constant. The
mechanism was right; the number needed a denominator big enough to be one. Measured properly it is
**9 of 200**, and that figure is now what sets the test's round count.

## 2026-08-30 (second entry) — what the fifth review round found, and why it kept going

Three fresh-context reviewers over the change I had just merged. They found four things, and none
of them was a defect in what the code *does* — two false claims and two untested paths.

**The rule that comes out of it: a review round that finds only false claims and untested paths is
the signal a design has settled, not the signal to stop reviewing.** Those are the cheapest defects
to introduce and the most expensive to find later, precisely because nothing goes red. Rounds one
to three found design defects and I kept going because things were obviously wrong; round five is
the one I would have skipped, and it caught a total loss of a bound.

**A fix for an off-by-one at one end of a range is not evidence about the other end.** My binary
search fixed a `repr`-escape being cut in half — a large-input bug — and created a worse bug at
small budgets: `text[-0:]` is the whole string, so when nothing fits the budget the function
returned the *entire* 100 kB document. The test I wrote with the fix exercised only the budget the
original defect had been found at. When a change touches a bound, test both ends of it and the
degenerate middle, not the value that motivated the change.

**A predecessor's tests are a checklist of properties somebody already thought were worth holding.**
I deleted `test_the_sync_path_announces_what_the_async_path_announces` along with the mechanism it
covered, and wrote no equivalent — so gutting the replacement's synchronous hook left 137 tests
green. Deleting a test is a decision that needs the same argument as deleting the code. Before
removing a test file's worth of coverage, list what each deleted test asserted and say, per item,
whether the replacement still needs it.

**A claim about ordering must be tested under the configuration that can falsify it.** "Innermost
of the governance chain" was false whenever the harness is enabled, and both tests pinning that
chain build it under profiles that attach neither of the two entries that nest inside. The
falsifying configuration was the one nothing constructed. When pinning a list that is assembled
conditionally, enumerate the conditions and pin the widest one.

**And the third round running in which I published a number I had not measured.** "~20 lines
replace ~180" — a 9x reduction against a real 2.7x (113 against 42, AST-counted). The previous
entry in this file is about exactly this, written by me, one round earlier. Writing the rule down
did not stop me doing it; what would have is counting before typing the sentence. **Treat any
ratio, count or size in prose as requiring a command in the same edit.** If there is no command,
there is no number — write "smaller" instead.

## 2026-09-04 — a gate that cannot run, and a contract with two halves

From the fifteen-review deployment-readiness pass (`D-2026-09-04-fifteen-fresh-contexts-over-one-tree`).

**A skipped gate is invisible in a way a failing one is not, so check what a green run did not
run.** `helm` is absent from this sandbox; twelve chart assertions skip as "helm is not installed";
five HIGH chart defects had survived every previous review because nobody had rendered the chart.
`tests/pg.py` has an epilogue that names its skips for exactly this reason and nothing does the same
for helm. **Rule for myself: before reporting a suite green, read the skip list and ask what each
skip means the run is not evidence about — then install the missing thing rather than noting it.**

**A green server suite is not evidence that a client can call the server.** Every one of twenty-seven
route tests wrote the request body the *server* expected, so an endpoint its only client could not
call stayed green through `mypy --strict`, ten validators and 6,406 tests. **Rule: where a route's
only caller is a companion repo, read that repo's call site and test the literal body it sends.**

**A per-package check is not the gate.** Nine fixers each ran `mypy --strict` on their own `src/`
package and reported clean; eleven errors were in `tests/`, which the real gate covers. Three more
regressions surfaced only in the full suite. **Rule: run the project's own composite target before
claiming done — never a scoped approximation of it.**

**Verify a claim against the thing it describes, not against another document.** A `BACKLOG.md` row
said a fix was blocked on "a matching change in `Chemclaw3_ui`" — that half had already shipped, and
believing the row is what kept the defect alive. A fixer found it by checking the client instead.
**Rule: a blocker written down is a claim about a commit; re-check it before accepting it.**

**When a reviewer prescribes a fix, measure the prescription.** Four were wrong in this pass, one of
them dangerously (`ABANDON` as a parent-close policy is worse than the default it replaces, not
better). **Rule: a finding's diagnosis and its proposed remedy are two separate claims and the
second is usually the less tested one.**

## 2026-09-05 — four reviewers over one day's measurement

From `D-2026-09-05-four-reviews-of-one-days-measurement`, reviewing my own #308.

**A comment that disclaims an anti-pattern is not a defence against writing it.** I wrote
`probes[::len // n][:n]` under a paragraph explaining why `probes[:n]` would be wrong, and for every
n above half the corpus those are the same expression. The comment made the code *look* considered
and stopped me checking. **Rule: when a comment says "not X", compute the case where the code most
resembles X and assert the difference.**

**I shipped an arithmetic bound with no test at all, in a change whose whole subject was
measurement.** Every other piece of #308 got tests; `--sample` got a paragraph. **Rule: a selection
or sampling rule is the one thing in a measurement that can be wrong while every arm still runs and
reports — it gets a test before it gets a comment.**

**Delegating a fix does not delegate the claim.** The false "CI never installed helm" narrative and
the unmeasured "33" came from a subagent's work I accepted because its evidence for the *mechanism*
was good. Its evidence for the *story around the mechanism* was a BACKLOG row. **Rule: when a
subagent reports a fix, re-derive the numbers and the causal claim separately from the diff — they
are two claims and only one of them was tested.**

**A claim is checkable against `main`, not against the branch point.** The ADR contradicting my helm
rationale merged while I was working. I never re-read `main` before merging. **Rule: before merging,
re-read what landed on the base branch since the branch point, specifically for claims my prose
makes about the same subsystem.** It happened again in the review itself, and cost more: three PRs
landed while four reviewers ran, and one of them rebuilt the judge on the provider seam — so the TLS
fix I had written, tested and described in an ADR was obsolete before it merged, and the merge
conflict was the only thing that told me. **Corollary: re-read the base *before* writing the fix,
not before pushing it, whenever a review takes long enough for the base to move.**

**Committing the result and not the artifact behind it is how a number becomes unre-derivable.** I
committed 442 transcripts and left the token counters in `/tmp`, so the one table nobody could check
was the cost table. **Rule: if a figure comes from a scrape, a counter or a clock rather than from a
file, commit the scrape in the same commit as the figure.**

**And the correction to a wrong assertion can be wrong.** My first fix for the sampling test
asserted "the last draw lands in the final tenth", which is false at n=2 where the correct
stratified draw is the first probe of each half. **Rule: state the property (each band contributes
one), never a proxy for it (the last one is near the end).**

**A test can be red only under `make cov`, and lesson 38's rule sends you the wrong way.**
`test_concurrent_batches_do_not_race_on_the_cache` failed the full gate and passed every time I ran
it alone — which reads exactly like the contended-load case above, and is not it. Coverage tracing
multiplies a tight 4,800-iteration threaded loop by ~30 (4.8 s bare, 132-216 s traced) and the
global cap is 180 s, so the run's outcome is decided by which side of that line the machine lands
on. Five isolated runs under `--cov`: 132 s, 152 s pass; 200 s, 213 s, 216 s fail. Perfect
correlation with the cap, none with the race the test is named for.

Two things I nearly got wrong. I reached for "order dependence" first, because that is what the last
two suite-only failures were, and it cost a detour. And the failure *presents* as the concurrency
bug under test, so "flaky race test" is the reading that offers itself — on a test whose own
docstring says a race test cannot promise to fail every run.

**Rule: reproduce a suite-only failure under the gate's own flags before theorising.** `pytest <file>`
is not `make cov`. And when a slow test sits under a shared cap, the number to check is its
*traced* runtime, not its bare one — this is the third test in this repository whose cap was sized
against a measurement nobody took under coverage.

**An absolute-sounding qualifier in a one-line design instruction is a question, not a spec.** The
owner wrote "things that change agent behaviour like skills only after human review". I read "only"
as covering every turn, wrote a whole ADR section rejecting the local-skills tier on that reading,
and was wrong: the review applies to the *shared* tree, and the local tier was the point of the
design. Both readings are grammatical and they differ in exactly one property — whether an
unreviewed skill may touch its own author's turns. **Rule: when a short instruction turns on a
quantifier (only, always, never, all) and two readings would produce materially different code, put
the two readings to the owner before writing either one up. The tell is that I can state the
distinguishing property in a single sentence — if I can, I can ask.**

**A rejected alternative's argument survives the rejection being overturned.** When the tier came
back, the objection I had raised against it (per-user skills fragment answers across chemists with
nothing recording why) was still true. Deleting it with the rejection would have lost the reason
the design needs an inspectability invariant. **Rule: when a decision reverses, re-file the losing
argument as a stated cost or as a requirement it generates — never delete it as though it had been
wrong.**
