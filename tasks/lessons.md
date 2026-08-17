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

2. **`Write` to a path that already exists destroys it.** Calling `Write` on `tests/test_graph.py`
   deleted 23 tests for the NetworkX indexer, and the suite still passed — nothing referenced them.
   Check for the file first. A green suite does not notice tests that no longer exist.

3. **A stale local clone is not evidence about a remote.** I told the user four of my own pushed
   commits "do not exist" and that a sibling session's work was gone; all of it was on `origin` the
   whole time and the container's clone was behind. Fetch before asserting anything about what is
   or is not on the remote.

4. **The working tree is not your baseline when other agents are in it, and `git add -A` is a claim
   about a tree you no longer control.** A verification pass reported a feature "already
   implemented" while reading another agent's uncommitted work. Three substantive fixes were built
   twice by concurrent sessions on one branch. A review subagent mutating the tree had its mutations
   swept into a commit by `git add -A`. Diff against a named commit, stage by path, and treat
   another agent's own "gate is green" report as a claim, not evidence — one such package arrived
   five lines over the lint limit.

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
   test by breaking the code it guards.

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
