# Lessons — the rules

Read at session start. Each rule is an imperative, one or two lines; the incidents behind them are in
git history (`git log -p -- tasks/lessons.md`) and `docs/archive/lessons-2026-0{8,9}.md`.
After a correction, sharpen an existing rule or add one — never a dated section. A rule broken
twice gets a mechanism (hook, test, `make` target), not a longer paragraph. Older code citing
"`tasks/lessons.md` rule N" refers to the pre-2026-10-07 numbering in git history.

## Working tree and git

1. Never `git checkout -- <path>`, `git restore`, `git stash`, `git reset --hard` or `git clean` while
   anything is uncommitted: commit first, or `cp` aside and back. `.claude/hooks/` blocks these verbs.
2. `Write` on an existing path replaces it — check first; a green suite never notices deleted tests.
3. Fetch before asserting anything about a remote; a launch receipt is not proof a subagent runs.
4. Other agents share the tree: diff against a named commit, stage by explicit path, and read
   `git diff --cached --stat` before committing — the index, not your `git add` list, bounds a commit.
5. Before fanning out, list what agents *share* (database, ports, index, caches), not only files;
   while a gate runs the tree is frozen and the shared database is read-only.
6. Use `git -C <repo>`, not `cd … && git …`; anchor `pkill -f`/`pgrep -f` patterns so they cannot
   match the calling shell.

## The gate

7. Run the gate's own command (`make lint type test`) at its full scope, in every repository you
   touched, on the exact tree you push; any edit after the last green run needs another run.
8. Never pipe a test run through `head`/`tail`; take the failing test's name from the final report.
9. Take the baseline before the first edit; "pre-existing" means shown red on the base, not assumed.
10. A skip is not a pass: start Docker/Postgres/Temporal first and read the skip epilogue.
11. Re-run a parallel-only failure serially before believing it; never run two suites, or `uv sync`,
    during a measured run; re-check `dockerd` is alive before trusting a result.

## Measurement

12. When two explanations compete, run it and report the number — with a null control and a
    repetition count; a ratio or a single reading is a smell, not a finding.
13. A handed-over claim (subagent, review, backlog row, earlier session, comment) is a hypothesis:
    re-probe it against current code before acting on or repeating it.
14. Measure at the layer the defect lives in — on the wire, through the outermost entry point
    production calls; a first call or a loaded-machine wall clock is not a cost.
15. When an instrument gives the same answer for both arms, check it is not blind: drive both arms,
    including the one you expect to pass.

## Tests and guards

16. Test a seam from the outermost thing production calls, and vary the fixture along the axis the
    defect lives on; tests written beside your code inherit its wrong beliefs.
17. A guard is not written until its mutation has been watched failing: assert the file changed,
    clear `__pycache__`, mutate one thing at a time, and read every survivor as a question.
18. A test over "every X" or asserting an absence derives its scope and asserts that scope is
    non-empty and is the right region.
19. A concurrency or deadline test asserts the situation it needs actually arose (lock contended,
    deadline expired); a module-level `asyncio.Lock` binds to the first loop that contends on it.
20. Never hard-`assert` a machine-dependent precondition in a test; derive it or skip with a reason.

## Fixing things

21. Analysing a defect is not fixing it: fix the root cause, then search for the same shape elsewhere.
22. A repair has its own failure mode, often the mirror of the defect; when consecutive fixes each
    cause the next defect, stop and redesign.
23. Making a value illegal, deleting a producer or moving a capability: enumerate every site that
    produces, reads or names it before declaring the change done.
24. A new column default is a claim about every existing row; a second producer of an event or
    metric changes the meaning for every reader.
25. A control that runs but cannot act reads as applied: prove the narrowing/limit/lock actually
    fires, and that a built capability is actually reachable.
26. Trust output files over exit codes, and a health route only for what it actually exercises.
27. Re-read the base branch before writing a fix, and verify a merge against a machine merge of its
    parents.

## Prose and records

28. Write no number, count or "cannot happen" into a non-test file unless a test fails when it goes
    stale; backticked paths are repo-rooted and checked; editing half a sentence re-verifies the rest.
29. Never tick a plan item, close a row or say "done" for work not done and verified; delete a
    backlog row in the commit that closes it.
30. Adopting a library for one property means owning its whole surface; a rewrap or bulk-edit script
    is code — diff its output before keeping it.
