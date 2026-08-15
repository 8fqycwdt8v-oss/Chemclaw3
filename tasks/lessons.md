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
