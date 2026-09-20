# Lessons — the rules

**Read this whole file at session start.** That is only possible because it is short, and it is
short on purpose. It has now been made short twice. The first time it was 1,937 lines across 80
sections, which is not readable at session start, so it was not read, so the rules did not fire.
The digest that replaced it opened with the paragraph below forbidding dated sections — and then
grew 88 of them, to 3,212 lines, **1.66× the length the restructure existed to escape**. Rule 1 is
the proof of what that costs: the *same* `git checkout` mistake is recorded fourteen times across
the two archives, and the sessions that destroyed work were reading the paragraph against it.

Every rule here is one paragraph. The incident behind it — the measurement, what was tried first,
what it cost — is in [`docs/archive/lessons-2026-08.md`](../docs/archive/lessons-2026-08.md) (the
first eighty sections) and [`docs/archive/lessons-2026-09.md`](../docs/archive/lessons-2026-09.md)
(the next eighty-eight), both unedited, both keeping the repeats. Go there when a paragraph is not
enough.

**After any correction from the user**, find the rule this belongs under and sharpen it, or add a
new one if the lesson is genuinely distinct. Do not append a dated section — that is how both
earlier versions of this file grew. If a rule is being broken repeatedly, the fix is a *mechanism*
(a script, a test, a `Makefile` target), not a longer paragraph.

**That prohibition is the rule this file broke, so it now has the mechanism its own paragraph
prescribes**: `tests/test_lessons_stay_a_digest.py` holds the file to its shape — every line under a
theme belongs to a numbered rule, no heading names an incident, every rule number is unique, and
every `rule N` / `cause (x)` citation in `src/` and `tests/` resolves here. A date in trailing
parentheses is what most of the 88 headings used, so the guard's subject is the *structure*, not the
spelling of a date.

**Rule numbers are stable labels, not an order.** Tests and modules cite them (`tasks/lessons.md`
rule 9); a rule keeps its number wherever it moves, which is why the numbers inside a section are
not contiguous — the same argument `CLAUDE.md` makes for the frozen `D-NNN` sequence. Never
renumber to close a gap; **31 was never allocated and stays unallocated.** A new rule takes the next
free number at the end of its section.

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

   **Recorded a sixth time, 2026-09-12, in the middle of a review whose own brief said "for every
   test you add or change, mutate the code it guards".** The mutation loop for the chart templates
   used `cp`/`mv` correctly four times and then reached for `git checkout -- tests/…` to undo a
   *fifth* mutation that happened to live in the test file itself — destroying ~250 lines of new,
   unstaged tests written that hour. Nothing failed: the suite went green, because the tests it
   would have failed were gone. So the rule needs its sharper form, which is about the *habit*
   rather than the command: **a mutation loop contains no git command at all.** Not `checkout`, not
   `stash`, not `restore`. The moment a git verb appears in a loop whose job is to damage and
   restore files, the loop can delete work instead of restoring it, and the only signal is a suite
   that got quieter. What made this one survivable was that the edits had been applied by scripts
   still in the session transcript; that is luck, not a procedure.

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

41. **A git verb inside a compound command runs wherever the shell has got to, not where you
    wrote it.** `git stash -u && git worktree add /tmp/basecheck <rev> && cd /tmp/basecheck && …;
    git stash pop` popped three uncommitted test fixes into the *scratch* worktree, and
    `git worktree remove --force` then deleted them; the next commit's message described three
    fixes that were no longer in the tree. Three mechanisms, in order of what they save: use
    `git -C <path>` and never `cd`, because `cd` persists across the whole command; never stash
    across an operation that changes worktrees, since a stash is repository-global and pops into
    whichever worktree asks — `git show <rev>:<path>` reads a base version with no such failure
    mode; and read `git status --short` *before* writing the commit message, not after. It printed
    one file where I expected four and I read past it, so the message I wrote described intent and
    read as verified.

42. **While a gate is running the tree is frozen, and the shared database is read-only.** A
    24-minute `make test` returned five failures, two of which were artefacts of my editing
    `analytical/stability.py` *during* the run — several tests walk the source tree at run time, not
    at collection. The cost is not the run, it is that a failure list mixing real defects with
    artefacts of its own conditions cannot be acted on, and the tempting move is to dismiss the ones
    that look unrelated. Same rule for the database: dropping and re-migrating a table under a
    running `make cov` made the whole run evidence about nothing. And a suite competing with a
    second suite is a timing measurement invalidated — see rule 38. Queue the edit, or kill the run.
    **Related, and the signature is distinctive:** an error in a file the branch never touched, on a
    table the branch never added (`relation "composed_workflows" does not exist`), means the local
    database is behind `main`. CI rebuilds from migrations every run and structurally cannot hit
    this; the sandbox database persists and drifts the moment a migration lands upstream. Run
    `make db-migrate` after every `git merge origin/main` — it took 7 ms and cleared nine errors.

43. **Start a long gate at the beginning of a session, and check whose signal killed it.** The full
    suite is hours here with Docker up; I ran it under a 900 s `timeout`, got exit 143, and reported
    the suite as failed. 143 is SIGTERM and 144 is my own waiter — check for those two before
    calling anything failed. And never `pkill -f <pattern>` on a pattern my own command line also
    contains: `pkill -f "pytest -q"` matched the harness's bash wrapper and killed my own shell,
    twice in one session, the second time *after* writing this rule down. Kill by pid from
    `pgrep -f "python3 -m pytest" | head -1`.

44. **Before fanning out, enumerate what the agents *share*, not only which files they own.** Three
    implementers with carefully disjoint source files still share one git index (rule 28's failure),
    one virtualenv, one database and one full-tree `make type` over 840 files — so any of them can
    chase or "fix" another's half-written module. Parallelism is free across repositories and
    expensive inside one checkout. **A reviewer that drives or mutates code is the sharpest case,
    because its restore is a write to my files:** one mutating `durable/template_job.py` restored
    from a `.orig` taken at spawn time, silently erasing an hour of my edits made since, and when
    stopped mid-restore left `if False:  # MUTATION` in a shipped file one commit from merge. So: a
    mutating reviewer gets its own worktree, or the files it was given are read-only for me until it
    hands back; and when one is stopped mid-flight, `git diff` its scoped files before anything else.
    A leftover mutation is indistinguishable from my own work in `git status`, and it is the one kind
    of leftover *designed* to make the tests pass while the code is wrong.

45. **Two environment claims to settle before writing code, both cheap.** Never name a scratchpad
    script after a stdlib module: a leftover `csv.py` in the scratchpad made `import csv` execute it
    on every chemclaw import (Python puts a script's own directory at the head of `sys.path`), and it
    clobbered `src/chemclaw/protocols/export.py` five times across two sessions while four
    diagnoses blamed subagents running `git checkout`. When a tracked file changes with no plausible
    writer, do not reason about who might have run git — put an `sys.addaudithook` on open-for-write
    against that path and print the stack. And **check write access to every repository a task spans
    before writing code in any of them**: I built both halves of a two-repo change and found at push
    time that the session's scope covered one, so `main` declared eight tools no running server
    answered. One `git push --dry-run` at the start would have changed the plan. (`git push --delete`
    is 403 through the agent proxy even where `git push` works, so never claim a branch was deleted
    without reading the output.)

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

   **And ask what the number could have come out as, before running it.** A measurement executed
   correctly can still be about the wrong quantity, in two distinct ways this tree has one instance
   of each of — and the first draft of this paragraph filed both under the second, which passes the
   very case it cites. **A mediator** is falsifiable and answers a neighbouring question:
   `D-2026-08-12-a-supervisor-that-holds-every-tool-has-no-reason-to-delegate` measured delegation
   *rate* (2 of 15) where the question was whether isolation helps. That number *did* falsify
   something and did settle the flag — "the flag stays off for a different reason than it went off"
   — so the tell is not that nothing could have refuted it, it is that the quantity was a step
   removed from the one asked about. **A metric with no reachable falsifying value** is the other:
   `D-2026-08-13-a-subagent-is-spawned-for-isolation-not-for-a-tool-it-lacks` put 14/15 against an
   arm already at 14/15, no headroom to show the improvement it was run to find, and
   `D-2026-08-15-a-capability-that-ships-off-is-not-a-capability` records that two of those fifteen
   probes span two specialists and fail in *both* arms — a floor before any model ran — and that the
   accuracy it divided by was *delegated* turns, so one correct delegation read 100%. (Both the
   metric and the panel it scored were deleted with that ADR; the numbers survive only in it, which
   is why they are cited to the ADR and not to a symbol.)
   Outside the tree the second kind is clearest: a preprint (F.A.D.E.,
   doi:10.64898/2026.06.20.733481) leads with a QED of 0.85 against 0.46 for the reference ligand,
   where the generator samples a drug-likeness prior — so that number cannot come out low — and the
   0.46 comparator is an approved drug, while predicted affinity, the axis the generator does *not*
   optimize, moved the wrong way and appears only in the discussion. **Rule: before measuring, name
   both the quantity the question is actually about and the value that would falsify the claim. A
   number that cannot come out falsifying, and a number about a mediator, are different defects and
   only the second one's conclusion is worth keeping.**

7. **A measurement script is code, and an unrun one is a claim.** A cross-validation number
   travelled from an unrun script into an ADR and a maintained capability map. If a number is going
   to be quoted, the command that produced it must be re-runnable and must have been re-run.

8. **Analysing a finding is not fixing it.** I wrote exact diagnoses of two findings, including the
   fix each needed, and ticked both off; `git diff` showed neither file had been touched. Likewise a
   backlog row that says "this needs a decision" is a claim about the code — two such rows were
   simply wrong about what the code did. Check before deciding.

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

46. **A wall-clock number taken on a loaded machine is not a measurement, and one reading of a
    stable-looking quantity is not one either.** `compute_thermochemistry` read 115 s then 147 s
    against a 0.816 s baseline and I started instrumenting for a defect; two abandoned background
    `pytest` runs were pinning four cores, and the idle number was 0.856 s. Check `uptime` and the
    top consumers first, and where the question is "was this recomputed?", assert the **call count**
    — it is immune to load and is what D-011 claims. The second half cost a paragraph of invented
    physics: re-deriving `FORKSERVER_RSS_CEILING_MIB`, one reading of the `jinja2` arm at load ~10
    gave 121.9 MiB and I explained a "+5.8 MiB allocator scaling" from it; four repeats at load 1.1
    read 117.7–117.9, which is the increment the record already held. **Repeat every arm you are
    going to compare, not just the one you expect to move.**

47. **A number going into the record is run more than once and reported as what varied — and a
    figure recalled from the shape of the code is wrong about a third of the time.** Three published
    measurements were falsified on re-run: "reverted 20 times out of 20" became 93 of 100; a "2.61 s
    at the 4 MB cap" described a payload the same commit made impossible; "90.6 ms → 15.7 ms"
    reproduced only at 384 arms. Three timing claims written from a sense of what the code does were
    all wrong when measured (the slowest tool was the wrong one; a figure was four orders of
    magnitude out) — and every conclusion survived its correction, which is exactly what makes the
    habit dangerous, because the argument being right is what makes the number feel checked. **A
    count of my own work is a measurement like any other**: I wrote three from memory in one cycle
    ("13 deterministic verdicts" went stale in two hours; "21 new tests" against a measured 41 node
    ids over 36 functions; "~20 lines replace ~180" against an AST-counted 113 → 42). The mechanism:
    treat any ratio, count or size in prose as requiring a command *in the same edit* — if there is
    no command, write "smaller". And when two changes ship together and the outcome improves,
    disable the interesting one alone: here the boring `max_length` refusal did all the work the
    O(n²) → `Counter` rewrite was credited with.

48. **Take the number off the wire, not off a serializer you chose — and give a table one
    basis.** I measured the
    condenser's saving with `model_dump_json()` and shipped 9.1× in a commit, an ADR and a PR body;
    production goes through LangChain's `_stringify`, which falls back to `str()`, so the real saving
    was 2.7× and the `Field(exclude=True)` the measurement rested on did nothing. I had never once
    read a `ToolMessage`. If I cannot name the function that turns my return value into what the
    model sees, I have not measured it. **And two numbers on different bases do not compare however
    carefully each was measured**: `props` at 11.7 ms was a whole MCP round trip written beside
    `thermalsafety`'s 63.7 µs engine-only figure, so the table read as 200× when on one basis `props`
    is the cheapest entry by two orders of magnitude, and the exemption that looked least justified
    was the best justified. State the basis once, above the table, and derive every entry the same
    way; two bases means two tables.

49. **A handed-over measurement — from a previous session, a subagent, a review — is a claim, and
    re-running it comes before reading the diagnosis attached to it.** A handover reported
    `chemclaw_invalid_tool_calls_total` carrying no samples and named LangChain's streaming assembly
    as the seam to enter; the metric carries two per turn, that seam is sound at every step, and the
    cause was one layer away. An hour of work sat behind a scary label, which is a reason to measure
    that seam *first and cheaply* — four print statements settled it. Two of six reviewer-supplied
    numbers did not reproduce as described (a "concurrency defect" is 0/100 as a race and 100/100 as
    latency; a torn read needed `REPEATABLE READ`), and a rate I quoted as "2/25" measured 9 of 200
    when given a denominator. **And when a subagent's fix comes with a story, the diff and the
    narrative are two claims and only one of them was tested** — a false "CI never installed helm"
    and an unmeasured "33" both arrived attached to good mechanism evidence. A quoted *string* from a
    subagent is the specific thing to refuse: `"0 failures / 3 held"` cannot be produced by the
    component it was attributed to, and it reached four first-party files, an ADR and the ledger.

50. **Read the bound, not the counter — and a measurement's unit can move underneath the number
    derived from it.** I extrapolated a drain at ~43 chunks from `ingested=100` log lines; the real
    figure was ~108, because `_BoundedIngest` caps *entries fetched*, so the counter I extrapolated
    from reports a different quantity from the one that governs the loop. The sharper form:
    `agent_max_turn_billed_tokens = 300_000` was argued as "above the one runaway this tree has
    measured", a turn that billed 250,000 — but 250,000 was ~25 calls when a call carried ~10,000 and
    is ~3 calls at today's `PREFIX_BOUND`, so the backstop sat *below* the two guards it was meant to
    sit above and would have ended ordinary heavy turns. **A cap that must sit above another guard is
    derived from that guard, never chosen against a past measurement** — write the relation
    (`harness_max_loop_iterations × agent_context_token_budget`), pin it in both directions, and
    mutate it. And when a setting's justification cites a measurement, name the measurement's unit in
    the same sentence: "25 model calls at the ~10,000-token calls of the day" visibly rots; "250,000
    tokens" does not.

51. **A survey sentence of the form "X is the one case where this cannot happen" is the sentence to
    re-measure first.** I briefed an implementer with "`calc`'s bounds are not env-readable —
    constants only", taken from a survey; the implementer derived the set from the code and found 42
    bounds including `calc`'s two, on the server whose calls take minutes to hours. The survey had
    read module-level `int` constants and missed that the values flow from a `pydantic-settings`
    class with `env_prefix`, so **an `os.environ`/`getenv` grep cannot see an env-overridable
    setting** — an inventory of "what a deployment can change" built that way is short by exactly the
    mechanism a well-structured repository uses. An exemption scopes work *away* from a case and
    reads as diligence, so nobody checks it. **The mechanism: brief an implementer with the
    measurement and the anchors, never with the conclusion.** Three further framing claims in that
    same brief were wrong and were caught only because the implementer had room to re-derive.

52. **A ratio is a smell, not a finding — measure a candidate before pitching it, and take a
    removal's blast radius before planning it.** Asked to make the backend "lean to read" I pitched
    three fixes off two ratios (58% prose, a 165-module import fan-out); both ratio-derived ones then
    measured out at nothing — relocating the biggest docstring yielded **0 lines** with readability
    held constant — while the structural one, a single 483-line function, was the whole problem and
    went to 194. Prose that records a measurement is not overhead. The tell I read and ignored: the
    docstring distribution's median was 9 lines, which says the prose is healthy; totals do not
    answer "is this bloated". Symmetrically, "ungating knowledge is nine call sites" measured as 348
    files mentioning the gate, 15 importing it, 11 of 11 eval probe files grading the behaviour — and
    *all nine callers were knowledge*, so the task was deleting a subsystem rather than rewiring one.
    Grep for importers, for prose mentions, and for tests or evals that assert the behaviour, and
    report all three; the one that reframes the task is usually the third.

53. **A claim that something is *stable* carries its repetition count, and the count has to be big
    enough for the rate it excludes.** Two clean `-n 4` runs became an ADR headlined "the failure set
    is identical in every arm" and a change to `make test`'s default; the verification run taken
    *because* the default had changed failed two extra tests, and five runs put them at 2-in-5 and
    1-in-5. Two clean runs of an 8,800-test suite is an ordinary outcome for a 40%-per-run flake. The
    speed half of the same measurement was fine on two runs, because a wall clock is one number and a
    failure set is a sample. Write "identical in two runs", then decide whether two is enough for the
    decision being taken — when the decision changes a *gate*, it is not. **And after changing a
    default, re-run the thing the default governs**; that is the only reason this was caught.

54. **When a claim is about what a framework does with an argument you did not pass, the source
    cannot answer it — go and look for the artefact.** A `BACKLOG.md` row, an ADR sentence and a test
    all held that the helper graph has no checkpointer because it is compiled with `checkpointer=
    None`. `None` is not "no checkpointer" to LangGraph: a subgraph inherits its parent's saver
    through the run config, so every helper had been checkpointing onto its caller's — one keyword
    turned 18,944 kB of rows into 424 kB. **The test was the most convincing thing in the tree**:
    `test_the_helper_graph_is_compiled_without_a_checkpointer` parsed the AST, found no
    `checkpointer=` keyword, and read the very absence that *causes* the inheritance as proof
    against it. Before believing a "we don't do X" claim, ask what row, file or byte would exist if
    we did — here one `GROUP BY checkpoint_ns`. **And when a finding surprises you, grep for the
    *opposite* claim first**: `agent/checkpointer.py`'s prune already described the helper namespace
    in the present tense, so the honest finding was not "nobody measured this" but "two parts of this
    tree disagree", which points at which one to change.

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

55. **A test of a seam starts at the outermost thing production calls, never at the function you are
    proud of.** This is `measure the mechanism, not the outcome` one level up, and the reason the
    earlier form was not enough is that a projector **is** a mechanism — what makes a mechanism the
    right one is not that it is concrete but that *something else in the system calls it*, and I get
    to choose my test's entry point the way I got to choose that serializer. Three instances, each
    one level up from the last. A shape test built
    `ConnectorJobResult(calc_refs=[...])` and asserted it round-trips, so it could not fail on a
    missing producer — and the first real run reported `calc_refs: []`. Seventy-two publish tests all
    started at `project()` and passed `payload_kind` by hand, which **no production call site set at
    all**, so the suite proved the projectors work and said nothing about whether anything reaches
    them; `grep -rn "<the hook>" tests/` returned zero and would have shown it in one line.
    `_TurnLedger.note_event` counted correctly under tests that called it directly, while the three
    columns it fills were `NULL/false/0` on every row a deployment could ever have written, because
    the event is built in `run_turn` rather than streamed. **If I cannot name the production caller of
    the function my test invokes first, I have tested my own intentions.** For any change that adds a
    reader — a column, a counter, a gate, an index — the first test drives the production entry point
    and asserts on what the *sink* received; a test that calls the reader directly may exist, but
    never alone and never first. **And for a seam with more than one part, one test assembles all of
    them against something real** — a database, not a fake. Assembling the publish path failed twice
    on things no unit test could see: a `@runtime_checkable` Protocol check requires *every* member,
    so the one driver failed the one sink at connect, and every drain pass leaked a connection
    because "build the sink per run" and "hold the connection for its life" are each correct alone.

56. **A fixture that never varies is a test that never tests, and the axis it holds constant is the
    axis that breaks.** Five defects in one body of work, five held-constant axes: all conditions
    present or all absent, never a mix (four fabricated condition changes); every extraction succeeds
    or one fails in isolation (two phantom solvent swaps); every document under the limit or one over;
    every reference resolves or none does; every cell first-party, never one that tries to *be*
    structure (a forged table row). Absent-vs-present, fails-vs-succeeds, small-vs-over-the-limit,
    homogeneous-vs-mixed account for all five. Before writing a fixture, list the branches the
    function takes and build the collection so its members differ on every one; where the function
    renders text someone else wrote into a structured format, one member's content must try to be
    structure. **The sharper question is not "does the fixture vary" but "does it carry a value the
    subject can change":** three of four surviving mutations in one wave were fixtures that made the
    subject invisible — a fake warehouse computing the watermark's semantics itself, so a reverted
    `watermark_expression` passed; a fingerprint record with no `source`, so a citation was bare
    either way. Where a fake deliberately duplicates the subject's semantics, pin the clause the
    subject emits somewhere else, in both directions, and say in the docstring why the duplication is
    safe. **A fixture may also *supply* the subject:** a guard for two new model fields was green with
    both fields deleted, because its fixture used `model_copy(update=…)`, which assigns *past*
    validation. For anything whose subject is a boundary — `extra="ignore"`, a decoder, a parser — the
    fixture crosses that boundary the way production crosses it, or the test is about the constructor.

57. **The tests I write beside my own code inherit the belief that produced the defect, so the
    searching has to be done by something that did not write the code.** This is the single most
    expensive recurrence in this file — recorded in at least fourteen of the 88 archived sections, and
    the ratios are consistent: seven fresh-context reviewers found ~30 defects in a change I had
    already reviewed and shipped green; six found 37 more in code four of my own review cycles had
    passed; fifteen found five HIGH chart defects nobody had rendered the chart to see; nine of
    fifteen defects in one tier sat under passing tests of mine, four of them checks that **could not
    fail**. The mechanism in almost all of them is a docstring stating the correct control beside code
    that does not implement it — not a control I forgot, one I *described*. My own three clearest:
    `test_mcp_face.py` imported `chemclaw_agent` to populate the registry, and that import is exactly
    what production lacked; `test_operations.py` wrote its adversarial marker into four columns the
    reading never selects, while seeding the one attacker-influenceable column with a safe literal;
    `test_units.py` proved case separates molarity from length on `M`/`m` and `mM`/`mm`, the two rungs
    that were right, while `nM` resolved to nanometre. So: test the production entrypoint rather than
    a convenient import of it; seed the adversarial value into the field the code actually reads;
    when a mechanism has a series — a prefix ladder, a state vocabulary, a status set — test the whole
    series or state which rungs are untested; and when a docstring names a control, go find the code
    that enforces it, *especially* your own from an hour ago. **Rule 14 does not apply to prose I
    wrote in the same breath as the code**, and the fan-out is one message: reviewers with fresh
    context, scoped by failure domain, over the diff.

58. **When a change adds a predicate, an index, a bound or a condition, enumerate what it cannot
    read before writing the test.** Not "add an edge case" — enumerate the domain and cover the part
    outside what the happy path constructs. `jsonb_array_elements` raises on a JSON `null`, and one
    such row in a table the sweep does not even erase aborted every actor's erasure permanently,
    under eight new tests three of which were proven red against the pre-fix code; for SQL over a
    payload that means every `jsonb_typeof`, and for an anti-join every reason the join might not
    find a row, including the rows nothing owns. For a *check*, enumerate the input classes and name
    the one the check exists for: `canonical_smiles("CCO junk")` succeeds and returns a smaller
    molecule, which is the entire class the guard existed for and the case a both-directions test
    written from one idea never reaches. **Three corollaries, each its own recurrence.** A test suite
    that only exercises the failure path proves nothing about the success path — a new event fired on
    turns that *succeeded*, painting two red rows above a good answer, and
    `test_a_repair_that_works_announces_nothing_because_nothing_was_lost` is the test that should
    have existed before the feature. A fix at one end of a range is no evidence about the other:
    `text[-0:]` is the whole string, so a large-input fix returned the entire 100 kB document at small
    budgets. And a claim about an assembled list is tested under the configuration that can falsify
    it — "innermost of the governance chain" was false whenever the harness is enabled, and both
    pinning tests built profiles that attach neither nesting entry.

59. **A concurrency or deadline test asserts that the situation it needs actually arose.** A test
    racing two real Postgres connections passed six times out of six *with the retry removed*,
    because it released the other transaction before the delete under test took any lock, so no cycle
    ever formed; a hand-written probe with the release gated on the other side being known to hold
    its lock deadlocked 16/16. "Exactly one of the two transactions was aborted" is one line and
    turns a no-cycle run into a failure. And when the outcome depends on *which* party loses a race
    nobody controls, split it: race the mechanism, inject the response. **A deadline separates two
    outcomes, not two speeds.** `test_two_workers_claiming_at_once_split_the_queue` proves
    `FOR UPDATE SKIP LOCKED`, where the only thing time observes is blocked-or-not: unblocked is
    0.9 ms, blocked holds to the 30 s statement timeout, and the bound was 10 s — four orders of
    magnitude above the passing case and still tight enough to fire on connection acquisition under
    load, a *third* outcome the assertion cannot tell from the defect. Put the bound between the two
    outcomes, as far from the passing case as the real backstop allows, and widen it only with the
    defect arm driven (deleting `SKIP LOCKED` still fails at 25 s).

60. **Reproduce a suite-only failure under the gate's own flags before theorising, and a new flake in
    a change that touched the subject is a message about the change.**
    `test_concurrent_batches_do_not_race_on_the_cache` passed every isolated run and failed the gate:
    coverage tracing multiplies its 4,800-iteration threaded loop by ~30 (4.8 s bare, 132–216 s
    traced) against a global 180 s cap, and five `--cov` runs correlated perfectly with the cap and
    not at all with the race the test is named for. `pytest <file>` is not `make cov`, and when a slow
    test sits under a shared cap the number to check is its *traced* runtime. I reached for "order
    dependence" first because that was the last two suite-only failures, which cost a detour — and the
    failure *presents* as the bug under test, so "flaky race test" is the reading that offers itself.
    In the frontend the same rule ran the other way: I read a ~50% flake as "this test's window is too
    tight" when my own change had cut the concurrent-stream cap's first wait from 15–30 s to 1–2 s.
    **Assume the change until the diff proves otherwise.**

61. **Take the baseline before the first edit.** A subagent reported a named test as "failing on a
    clean baseline too — pre-existing, unrelated"; a `make lint type test` I had started on the
    untouched tree before any of them reported came back 5,444 passed, exit 0. Believing it would have
    cost a whole class of failure classified away in advance, since every subsequent red in that file
    reads as "the known pre-existing one". It is one backgrounded command at the moment work begins,
    and it is the only artifact that separates "my change broke this" from "this was already broken"
    without argument. **And for every test that fails after a redesign, name which of the two it is** —
    the assertion is obsolete, or the guarantee it encoded is one I just broke — and say so in the
    commit. The poisoned-index test failed because the gate's linked worktree has its own index while
    my writer shares one; "the design changed, update the assertion" would have shipped that.

62. **A test that asserts an *absence* is only as good as the region it reads, and the region must be
    proven rather than intended.** I asserted `durable/check_in.py` contains no model-running call and
    implemented it as `source.split('"""')[2]`, meaning to skip the module docstring; that slice ends
    at the *next* docstring, so it covered 1,636 of 9,833 characters and left everything below the
    first class — where such code would actually live — unguarded. Both versions pass on a clean file,
    which is what a guard does and why passing says nothing. Two cheap fixes: parse the tree
    (`ast.walk` covers every statement and skips docstrings for free, because a docstring is a
    `Constant` carrying no `Name` or `alias`), and **plant the violation where the implementation
    would really put it** — the bottom of the file, not the top. **And deleting a test is a decision
    needing the same argument as deleting the code:** I removed
    `test_the_sync_path_announces_what_the_async_path_announces` with the mechanism it covered and
    wrote no equivalent, so gutting the replacement's synchronous hook left 137 tests green. A
    predecessor's tests are a checklist of properties somebody already thought worth holding; before
    removing a file's worth of coverage, list what each deleted test asserted and say, per item,
    whether the replacement still needs it.

## Mutation testing and guards

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

63. **A guard is not written until its mutation has been watched failing, and "the suite is green
    after I mutate" is a finding about the guard, never about the code.** Recorded in six of the
    archived sections and the ratios are grim every time: nine of one merged tier's controls were
    hollow and each had passed review; 30 mutations against one day's tests left 7 surviving, every
    survivor asserting *the shape of a thing rather than its effect* (a pool count instead of which
    server each pool dials; a substring of a PromQL rule instead of what it computes; a difference
    between two renders instead of the number rendered; three of a chart's four fleet inputs; two
    sweep points on the same side of the constant they pin). Mutate the fix and watch the test fail
    **before writing the commit message** — the one line the test is about, not the whole suite.
    A test and the fix it guards, written together, share a blind spot, and none of these substitutes
    anything: they observe the real object and ask it the wrong question. **Reading the test is not
    evidence.** The tell: if I can state what the test asserts without saying what would break, it
    asserts a file. And knowing the pattern does not catch it — two of the nine were controls written
    in the same session while explicitly reasoning about this failure mode.

64. **When a mutation survives, do not reach for a bigger mutation — ask which of these seven the
    assertion is.** A bigger mutation that still goes green tells you nothing about which branch you
    are in, and each has a different repair, so "assert something stronger" fixes only (b).

    - **(a) The assertion is about an outcome the harness cannot produce.** `"x-injected" not in
      response.headers` — ASGI carries headers as a list of pairs, so a CRLF never splits in-process
      and the assertion passes with no sanitiser. Assert on the *character*.
    - **(b) It names a word the code does not use.** A digest test asserted `"disputed"` absent while
      the notice says "disagree with something already in the graph". A guard against an extra line
      has to be about the line.
    - **(c) The collection it iterates is empty.** `load_profiles()` returns what it newly
      *registered*, so the second call in a process returns `[]`; the loop ran over nothing and passed
      over a defect measured minutes earlier in a plain interpreter.
    - **(d) It drives one call where the defect needs two.** `..._is_reported_once_rather_than_never`
      asserted a single `_is_new` call, so what it pinned was "every time, forever".
    - **(e) It is about the call's shape, not its argument's value.** An AST scan for `minted_on` in
      the keywords passes when every call passes `minted_on=None`; assert `ast.unparse(value)`.
    - **(f) The scan's universe is a strict subset of the surface at risk.** `registered_tools()` is
      31 tools; the agent binds 114, and the excluded set held the exact bundle the guard was about.
      **(c) is the degenerate case of (f)** — same object, same repair — kept separate only because
      the tell differs: (c) is a loop body that never runs, (f) one that runs over the wrong things.
    - **(g) Existence stands in for reachability.** A symbol can exist, be referenced and not be
      served: `@activity.defn` without `@durable_activity` wedges a replay exactly as deletion would.
      Assert the registration, not the symbol.

    So (a), (b) and (d) want a different payload or a second call; (c) and (f) want a different
    universe; (e) wants a different assertion; (g) wants a different property. **Every assertion in
    all nine was *true*** — each true for a reason other than the behaviour it was supposed to hold,
    and three sat under a confident paragraph explaining exactly what the guard was for, which made me
    slower to suspect the test. **And "driving this would only test the fixture" is the sentence that
    precedes a vacuous guard**: it is occasionally true and mostly a reason not to write the harder
    test. Both guards whose docstrings said it were 20 lines away from being real, and one re-committed
    cause (e) *inside the commit that added cause (e) to this list*.

65. **A mutation loop asserts the file changed before it runs the test, and every SURVIVED is a
    hypothesis while every RED is evidence.** A mutation that fails to apply is indistinguishable from
    one that survived, and the reassuring reading is the one that needs no follow-up — recorded three
    times: a `str.replace` anchored on `import asyncio` in a file with no such line ("that test is
    vacuous"; it went red the moment it was re-anchored); four regex guards driven through single
    quotes so bash handed Python both backslashes and nothing was substituted, reported as four
    covered guards when each could be deleted with the file green; an unquoted heredoc mangling a
    third. Four identical "16 passed" lines for four different substitutions is the shape of a loop
    that is not substituting. The fix belongs **inside the loop, on the bytes** — read the file, apply
    the substitution, assert the new text differs, *then* run pytest — not in the reading of its
    output, because the reading is where the optimistic interpretation lives; where `sed` or bash does
    the edit, `git diff --numstat` on the path before the test runs, read for a non-zero count. **A
    mutation is checked against the test it was written to redden, not against the count.** An
    inverted SQL string produced invalid SQL twice and reddened *thirteen* tests each time, which
    reads as a mutation caught emphatically and was a broken fixture reaching no subject; the valid
    mutation reddened exactly one. For SQL, run the mutated statement once — a syntax error is
    indistinguishable from a finding in a pytest summary. And a mutation reddening a test it has no
    business touching is a signal about the *tree*, not about the test.

66. **A guard is finished when *other* mutations of the same property have been watched still failing
    — at least two, and at least one of them a reword rather than a deletion.** The first mutation
    proves the guard is connected to the defect; the others map its boundary, and the boundary is where
    the next instance lands, because the next author is not re-introducing my mutation, they are
    writing a sentence. If the only mutation that reds is the one the guard was written for, what
    shipped is a regression test for a fixed bug, which is a smaller and different thing than a guard.
    Measured: seven mutations against two guards I had written and hardened the day before left **six
    green over a live, false statement** — a subject population of *backticked* tokens, so a reword
    that dropped the backticks removed the row from the owed set as well as the claimed set; a pairing
    window of one sentence, so a full stop was an exemption; one adjective between the cardinal and
    "tools" evading the pattern a comment said covered it.
    **Rule: derive the scope, do not assert that it is non-empty.**
    Both prose-guard holes were an unanchored string and the first fix was a
    stronger assertion over the same string; the assertion is the weaker move every time — `find_spec`
    on the module that defines the surface, or the `name:` its own manifest declares, makes a rename
    *carry* the scope instead of emptying it, and leaves the assertion to guard only the residue
    resolution cannot see. Say which residue that is. **State a guard's narrowness in the same breath
    as its rule, and prefer widening when the false-positive cost measures zero** — three of the six
    holes were narrownesses the docstring described honestly and the `#:` comment then overstated, and
    a reader believes the comment. **And an exemption is exempt from something: name what.** "This file
    quotes the sentences it refuses" licenses the *quotations*, not the file, and the implementation
    licensed the file.

67. **Commit before you mutate; mutate in the foreground, one at a time.** A commit costs nothing and
    makes every revert verb safe, which removes rule 1's hazard entirely rather than managing it. The
    two failure modes that are not about the git verb: a mutation harness is a patch-run-restore
    *pair*, so anything that can kill it between the halves leaves the tree looking edited by me — a
    backgrounded batch was cut off mid-iteration, left `"model_route"` -> `"model_routes"` in
    `turn_graph.py`, and its re-run then backed up the *already-mutated* file and dutifully restored
    the mutation, carrying the defect forward in the backup itself. So make the harness refuse to start
    when a backup file already exists: a stale backup means the last run died, not that this one may
    proceed. **And a restore script must assert it found what it was undoing** — print-on-success
    outside the conditional is how a no-op reports as a fix, and `str.replace(old, new, 1)` hits the
    first occurrence, so a guard written twice (once per hook) was mutated on the sync path while the
    test drove the async one and read as unpinned when it was pinned.

68. **A control's subject must be a behaviour, not a file — and a control's fixture is part of its
    subject.** `assert "save_local_skill" not in inspect.getsource(proposal_tools)` is a claim about
    where somebody chose to put the code, and a registered tool doing exactly the forbidden thing,
    appended to that same module, left it green. So is `{name for name in registered_tool_names() if
    "accept" in name}` — a claim about vocabulary. So is an assertion on the in-process ledger one
    layer short of the row the guard reads, where two one-line mutations of the producer left 467 tests
    passing. And `predicted_helper_surface` compared against a build calling the same two functions is
    "a basis that is re-derived rather than observed will agree with itself forever" — happening to a
    test that *cites that sentence as its reason for existing*. The second half cost three more in the
    fix itself: a registry walk passed alone and failed the first full run because it walked a registry
    `_register_generated_tools()` had never filled, so a control written to cover *every*
    tool-defining module covered the ones a bare import reached; and two tests asserting an "empty
    corpus" ran against a database 243 other tests write to. **"It passes" and "it passes on the
    shipped surface" are different claims** — ask what the fixture builds and whether that is what the
    docstring's sentence is about, then run the whole suite, because a green targeted run is exactly
    the evidence that misses this.

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

69. **A backticked path is repo-rooted or it is wrong, and a file name written from memory is a
    claim this tree checks.** `tests/test_docstring_paths.py` caught four of my sentences in one
    session: `kg/note.py` cited `tests/test_graph_analytics.py`, which does not exist — a fact I had
    *established by grep ten minutes earlier* and then wrote from memory anyway — and the next commit,
    one entry after writing the rule down, said `warehouse/binding.py` for
    `src/chemclaw/ingest/eln/warehouse/binding.py`. So "paste it from the shell" is not the rule: the
    failure is writing **the shorthand a human would say out loud**, which is not what the file is
    called. Before committing prose that names a path, run `make lint && .venv/bin/python -m pytest
    tests/test_docstring_paths.py -q` — five seconds. **And after any rename or deletion run the prose
    validators**, not the suite: `test_docstring_paths` found 22 files pointing at modules I had
    deleted and `prose-validate` found seven more, including a setting name inside my own replacement
    text explaining that the setting was gone. Neither mypy nor ruff nor the tests see a docstring
    pointer.

70. **An edit that corrects part of a sentence re-verifies the rest of it, or deletes the rest.** A
    probe header said "`thermalsafety` (8851) and `kinetics` (8852) are `next`"; `thermalsafety` had
    shipped, so I rewrote the sentence as "`kinetics` (8852) is **still `next`**" — and `kinetics` has
    been `proposed`, never `next`. A commit whose entire subject was stale cross-repo claims shipped
    one, in the sentence it was correcting. Two things made it invisible: the word "still" reads as a
    *check* because it asserts continuity, and half the sentence had just been verified, which lends
    the other half a borrowed credibility. **"Still", "remains", "unchanged" and "as before" are the
    words to search for — each asserts a check, so each owes one.** And **do not transcribe another
    repository's status field at all**: record what was observed with the date it was observed, the way
    `Chemclaw3-mcp/CLAUDE.md` records this family's port assignments, because a status in a file no
    test here can read goes stale on somebody else's merge schedule and the present tense claims
    otherwise. The same shape one line over: `_invoke`'s at-capacity message was corrected for
    promising a retry only one of its two callers performs, while the `logger.warning` immediately
    above it went on saying "the job will be retried" through that whole commit and its review —
    **grep the claim rather than fixing the line.**

71. **Before writing a number into a non-test file, ask which test would fail if it went stale; if
    the answer is "none", the number belongs in the test and the prose gets the test's name.** I put
    "26 pools steady, 36 at the peak" into the exact `values.yaml` whose prose pin exists because it
    once said "17 pooled processes" over a render of 14, and the pin failed my commit — the system
    working, and the third record in this file of transcribing a measurement into prose. **A
    repository with a pin for this class of mistake is a repository that has already made it, so read
    the pin before writing the paragraph.** And **re-measure a figure at the end of the branch, not
    when you first take it**: a probe-coverage figure measured one day went stale inside its own merge
    range because a sibling commit in the same pull request added 36 probes — in a docstring citing
    `D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` two paragraphs above. A merge range is a
    commit too. Also: **commit the artifact behind a number in the same commit as the number.** I
    committed 442 transcripts and left the token counters in `/tmp`, so the one table nobody could
    check was the cost table.

72. **A new tool, job, note type, error class or shared constant touches *declarations* far more than
    it touches code, and the declaration guards are a different target.** A rotational-profile job
    passed its own tests, `mypy`, `ruff` and four validators, then failed the full suite **eight**
    ways, every one a guard on a declaration: `.env.example` mirrors every setting;
    `test_context_floor` caps the static prefix *and* refuses a single tool over 900 tokens;
    `test_probe_coverage` wants an eval probe per agent-callable tool; `test_solvents`,
    `test_templates` and `test_docstring_paths` each pin something; and a profile's `tool_names` is an
    **allow-list**, so the job was reachable and unusable until it was named there. Two were
    load-bearing — the tool arrived at 1,499 tokens because pydantic publishes a model's docstring as
    its JSON-schema `description`, and the allow-list would have shipped a capability nothing could
    call. Removing a capability is the same asymmetry from the other side: `make lint type test` was
    green with five live false claims still in the tree (two skills declaring a deleted tool, three
    backticked paths naming deleted files) and `make prose-validate` plus `make skill-validate` found
    all five. **Run the declaration guards by name the moment the new kind of thing exists**, and never
    call such a change verified on a targeted run — the interesting tests guard what a change
    *declares*, not what it computes.

73. **A "because" pointing at another module is a testable claim about that module, and the docstring
    is where it becomes invisible.** Three shapes of the same failure. A cost-is-affordable claim names
    a mechanism: `store.advanced()`'s docstring ended "which revision was approved stays recoverable:
    `set_status` records it", and `set_status` wrote one column and logged a line that did not carry
    the revision — I wrote the demotion and the sentence excusing its cost one screen apart, and the
    sentence was an invention. A **fix's premise** is a claim about code you did not write:
    `supported_from` was added to stop a digest re-notifying under "when a member joins, the id changes
    too, so the identity and the date move together", while `stable_id` hashes `min(member_ids)` and
    its own docstring spends a paragraph explaining why it deliberately does not hash the set — one
    `python -c` before the sentence was written. And **correcting *why* a control exists is not
    evidence about the control**: an ADR noticed a bound's stated reason was broader than its evidence,
    re-scoped the claim, corrected three docstrings and kept the behaviour without driving it; driven a
    fortnight later against two real servers, the re-scoped claim was false too (4 concurrent 1.88 s
    calls over one open tool object finish in 1.99 s). That ADR's own words were that the risk is "the
    next reader … leaves it in place believing a measurement covers it", and it then did that. If the
    remaining arm is drivable in one script, drive it in the same commit; otherwise name the arm you
    did not run, in the ADR, as a gap. **An absence used as a reason not to decide is the claim to
    check first**, because nobody checks the thing that lets them stop: "nothing counts how often
    `task` is called" was repeated in three places as a reason a decision could not be taken, and
    `chemclaw_tool_calls_total{tool="task"}` had always moved.

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

74. **A repair has a failure mode of its own, and it is usually the mirror of the defect.** Five
    fresh-context reviewers pointed at one wave's *fixes* rather than at the code they fixed found
    seven wrong and two worse than what they replaced: a trailing flush moved into a `finally` with a
    blanket `except` turned a loud failure into `wrote 4 note(s)` and exit **0** with nothing in git,
    on the *common* path; `BatchingNoteWriter.write` returning `written=True` at accept time made
    `chemclaw_notes_recorded_total` count notes that provably never reached the graph, under a metric
    declared as "Notes written into the knowledge graph". Under-counting became over-counting, a silent
    drop became a silent success, and a bound that admitted a selection effect became one that admits
    no data (a Monte-Carlo put the per-repeat delegation needed for an even chance of any report at
    ~87.4%, *rising* with the repeat count). **Before shipping a fix, ask what the inverse error looks
    like and check you have not just bought it** — one more mutation, "make the thing the fix depends on
    fail", would have shown every one.

75. **When consecutive fixes each introduce the next defect, stop fixing and look at the design.**
    Three rounds of "found a defect, fixed the defect" was itself the finding and I read it three times
    as three unrelated bugs; the fourth review ended in 113 lines of retry machinery replaced by 42 that
    move a call from one field to another. The diagnostic is not the count — it is that every fix
    *rebuilt something the surrounding system already provides*: a loop ceiling because the loop cap
    could not see the extra model call, a reporting ceiling because nothing bounded the corrective
    message, an announcement rule because no `tool_failed` could be raised, a call-id guard because the
    announcement had no id. Four hand-built substitutes for four things a graph iteration gives away. **A
    mechanism that needs its own version of what it sits next to is standing outside it.** The
    counterpart, which is the round I would have skipped: **a review round that finds only false claims
    and untested paths is the signal a design has settled, not the signal to stop reviewing** — those
    are the cheapest defects to introduce and the most expensive to find later, precisely because
    nothing goes red, and round five caught a total loss of a bound.

76. **After fixing a defect, search for its shape elsewhere — I fix the instance and write a docstring
    about the class, which reads as if I had fixed the class.** Every one of six strangers' findings in
    a tier my four reviews had passed was the fixed bug living one field, one function or one layer
    along: `_structures`' docstring argued that `reaction_smiles` says what is *asked for* while the set
    says what the design *does*, and left `request.components` in the same set — making the commonest ask
    in process chemistry ("get me out of DMF", which names DMF and forbids it in one sentence)
    permanently unstorable; `_number`'s docstring named `1.23457e+06` as the defect it fixed while the
    fix covered only integral values and the literal example still reproduced; `_cell` stopped free text
    restructuring a table and nobody asked the same question about the text *outside* tables, where a
    hazard string forged a second `## Waste` section. **And a fix can silently retire a test somewhere
    else**: a `FOR UPDATE` added later for an unrelated defect serialised the two writers that
    `test_two_writers_racing_on_one_head_lose_as_a_revision_conflict` exists to race, so the branch is
    unreachable, replacing the handler with a raised `AssertionError` leaves the suite green, and the
    test goes on passing while its docstring is false. Nothing signals this; for any test whose value is
    the branch it reaches, break the source and check it goes red.

77. **A queued row is a hypothesis — restate its rule in your own words and name the kinds of value it
    will apply to.** Two of seven `BACKLOG.md` rows worked in one pass specified the wrong change, both
    the same shape: a rule stated correctly about one kind of value and generalized to a kind it does not
    fit. "Compare a field only when both sides recorded it" fits a setpoint, where `None` means nobody
    wrote the number down, and destroys a species set, which is derived from a components list present
    either way — so an empty `reagent` set is the record saying *this run used no reagent*, the most
    common real change a run-to-run series carries. A credentials row named three fields; the class is
    seven, and the two it omitted were the interesting ones (`llm_fallback_api_key`, in no redaction list
    at all, and `framing_envelope_secret`, which is not a credential but is the key a forged envelope
    would be signed with). **If any two kinds differ in what "absent" means, the row covers two rules
    and I am about to ship one of them wrongly** — then run the existing tests for the function before
    writing new ones, because the test that disagrees with the row is the cheapest review there is. A row
    can be fresh and still wrong: it is one person's design sketch and the tree decides. **And a
    finding's diagnosis and its proposed remedy are two claims, of which the second is usually the less
    tested** — four prescriptions in one review pass were wrong, one of them dangerously (`ABANDON` as a
    parent-close policy is worse than the default it replaces).

78. **A falsified justification licenses re-measuring the thing, never removing it.** The comment
    defending `plan_cache_mode=force_custom_plan` said the dense vector query cliffs from a generic plan;
    it does not, and the mechanism it named is not how pgvector behaves — so the setting looked like dead
    weight kept alive by a plausible story, which is this repository's own deletion pattern. Measuring
    the *second* claim in the same comment found a real 1.81× regression on a different statement by a
    different mechanism, so the deletion would have removed a control that works. Check every claim the
    comment makes before acting on the first that fails. **The mirror case is riding a second problem
    rather than filing it:** building the condenser surfaced `read_corpus`'s full ELN rescan, and a
    derived store would have closed both — two problems, one store, built without anyone deciding to
    build it. Note frontmatter answered the condenser with no new store and the rescan is a row with its
    own trigger. When one change would close a second unrelated problem as a side effect, check whether
    the second problem is driving the design. **And if finishing a task as specified would delete or
    orphan a working subsystem the spec did not name, stop and ask** — "delete it" and "keep it dormant
    for the decided-but-unbuilt subject" are both defensible and cost very different things.

79. **When a change makes a previously-legal value illegal, the search is for every site that can
    *produce* that value, not for every site that names the type.** Tightening `_check_classification` to
    refuse an empty `tools` list broke five tests in a file that *was* in my first grep's output — I saw
    `tools=list(allowed)`, read a variable being passed through, and never asked what `allowed` defaults
    to, which was `()`, the exact value the change makes illegal. Grep the constructor, then read each
    hit to the point where the argument's value is *decided* — a default, a fixture, a parametrisation —
    rather than to where it is passed along; four of the five files that broke had nothing to do with
    connectors. **The same move applies to strengthening a type:** hardening `llm_api_key` to
    `SecretStr` would have silently disabled log redaction for every credential, because both readers
    test `isinstance(value, str)` and `str(SecretStr("k"))` is `"**********"` — so the filter would have
    gone on matching asterisks and reporting success. Two protections that look like one, where the
    stronger-looking one turns the other off; grep every `isinstance` on the old type first. **And two
    value-domain traps worth naming on their own.** Never use `or` to default a numeric that can
    legitimately be zero: `abs((rx.yield_percent or -1) - want)` reported 21 of 400 yield mismatches
    where there were none, because 236 of 3,955 published wells are exactly 0.00% — a *real* result
    meaning the combination failed. (The tell was that 21/400 ≈ 5.3% matched the corpus's own 6%
    zero-yield rate too well; when a defect rate is suspiciously close to a *proportion of the data*,
    suspect the measurement.) And never zip two lists by position unless one is documented as derived
    from the other: `ReactionEnergyResult`'s `species` list is produced independently of
    `reactants`/`products` and is empty at `quick` level, so an index match attached cyclohexane's free
    energy to butadiene — silent by construction, since both values are plausible energies in the same
    units on the same reaction. Match on identity, and make the fixture *disagree* on order deliberately.

80. **A structural claim in a plan is a claim, and the function body is what runs.** "Pistachio is zero
    new code — one manifest" ignored that the `vector:` half runs `VECTOR_COSINE_SIMILARITY(col,
    ?::VECTOR(FLOAT, n))`, which is Snowflake's function *and* Snowflake's type, and that Databricks has
    no array parameter type at all, so a 1536-float query vector cannot be bound as a list on any
    statement. "The vendor shapes go in `tests/test_upstream_surface.py`" ignored that its assertions
    import their package unconditionally, which would have made the suite depend on clients deliberately
    not installed. Both were plausible because I had read the *neighbourhood* — the seam's README, the
    sibling adapter — and inferred the rest; for a test file, read its docstring's statement of what
    belongs in it. **The same rule against reading instead of running holds for models and numeric
    contracts.** Seventeen projectors written from a careful read of `science/calc/models.py` had three
    wrong: `Conformer` has no `energy_hartree` (only `EnsembleMember` does), `EnsemblePayload` has no
    `smiles`, and `DescriptorProfile` has a `fraction_csp3` I had simply not seen — and the third's
    failure mode is *silence*, because a field nobody publishes looks exactly like a field nobody has. A
    projector is not written until it has been run against an instance, and the stronger form is a
    coverage check: record which keys were read and diff against `model_fields`, which found three more
    gaps and is now `test_every_model_field_is_read_or_deliberately_ignored` with an exemption list.
    **Wherever one model is projected into another, assert the mapping is total or explicitly partial**;
    a partial mapping nobody declared is indistinguishable from a complete one. And when adapting a
    vendor to a numeric contract, look up what the number *means* and write down the boundary values:
    Databricks' score is `1/(1 + d²)` over Euclidean distance and `VectorMatch.score` is contractually a
    cosine, so nothing would have raised and a corpus would have been ranked slightly wrong forever.
    **Finally, a bound's name is prose too.** Four BO defects were one shape — a quantity that is checked
    and a quantity that is spent, differing by a factor nobody multiplied — and `bo_max_rounds` reads as
    a cost ceiling while being a loop counter. My own fix for one of them shipped with the same defect
    *inside it* (it stopped multiplying once the running product passed the ceiling, and a partial
    product shifted right by `n_generators` lands back under: 40 two-level factors at one generator
    passed a 4,096 ceiling against a true design of 2^39 rows). I caught it by writing the arithmetic
    into a script and running it, not by re-reading the code.

81. **A fix that depends on a system behaviour is not done until that behaviour is measured, however
    clearly the code reads.** I re-targeted a probe from an unreachable dataset to a reachable one on
    the assumption that *reachable* implies *findable*, then noticed the ingested notes were unmerged
    proposals — 39 note files against 2,000+ ingested reactions — and that
    `FingerprintReactionRetriever._eligible` drops matches whose note is not on disk, which reads like
    the fix does not work. Running it showed **both** are true depending on the path: an unfiltered
    `similar_reactions` returns 10 real wells and the same search narrowed by `{"type": "reaction"}`
    returns 0, so the re-target holds — but only because I ran it, and the opposite conclusion was
    equally available from reading either function alone. **Two correct functions compose into a
    consequence neither docstring states.**

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

82. **A health or readiness route is evidence about a narrower thing than you assume, and an error
    message is the caller's belief rather than the subsystem's state.** `/readyz` reported every
    connector `healthy` while two things were broken underneath: `chem`/`safety` read healthy because
    `/healthz` is unauthenticated and the front door held no token, so every `/mcp` call was rejected and
    turns degraded with nothing naming a credential; and `calc` was *down* while `/readyz` was entirely
    green, because `calc` is dialled from inside a tool rather than probed. **Never infer "the dependency
    is up" or "the caller is authorized" from a health route** — when a probe says healthy and a turn says
    otherwise, the turn is the measurement. Symmetrically, four storm checks failed with "the calculation
    service is not answering" and I was one step from filing a Temporal durability defect; the server's
    access log said `401 Unauthorized` on every call. Verify a named subsystem state *at the subsystem* —
    and note that **a retryable error class is an assertion that waiting helps**, so misclassifying a 401
    as an outage spends the whole retry budget proving the same thing.

83. **A red check is a claim about the system *or* about the check, and on this tree the odds are close
    to even.** Six findings from one four-repo campaign: three were the check. `prose yields its numbers`
    failed 0/12 and read like a broken extraction while asserting the *opposite* of a merged ADR, so
    "fixing" the adapter would have made the system violate `D-2026-08-26-a-transcription-may-not-infer-
    a-setpoint`; `f-malformed-json` demanded a truncated argument document be reported, which LangChain
    repairs through `parse_partial_json` before anything first-party sees it; and four storm failures were
    my own missing `CHEMCLAW_MCP_REPO`, surfacing two families later as unrelated red rows. **Before
    fixing what a red check points at, ask what the check asserts and whether the system is documented to
    do that** — read the module's docstring and grep `docs/decisions/`. A check that has never passed is
    evidence about the check. **And re-run once with the environment fully set before believing any
    failure**: a lane failure and a real failure are both a red row with a plausible observation, and the
    re-run changed the verdict on three of four. The same caution applies to a *permitted-looking pattern*:
    two migrations on `073` looked fine because `core/migrate.py` keys the ledger on the full filename and
    `main` already carries `037_*` and `043_*` pairs — both true, and the four existing files are
    **grandfathered by name** in an exemption list whose docstring says it exists "for the next one".
    Reasoning from precedent led me toward the one move the rule was written to make expensive. **When the
    tree shows a pattern that looks permitted, find the gate that governs it**, and grep the *subject*
    rather than the vocabulary you would have used — this repository names its checks as sentences, so a
    keyword search over identifiers is a weak instrument here.

84. **Get the failure's name from the run.** Mid-run streaming output showed one `F` at the 16% mark;
    rather than wait twelve minutes for the summary I mapped 16% onto `pytest --collect-only`'s ordering,
    landed on `test_connector_transport.py`, ran it alone, saw it pass, and recorded a timing flake under
    load. The file was never the failing one and its passing was evidence about nothing — the real failure
    was a new setting I had not documented in `.env.example`. **A percentage in a progress bar is a
    position in an ordering I reconstructed, not an identifier**, and running the guessed file can only
    fail to falsify the guess. Never let "flake" be the conclusion of a chain that starts with a guess
    about identity; it is the one classification that requires knowing exactly which test, since the whole
    claim is about that test's history. **And when a test harness consumes a build directory, the build
    flags are part of the fixture:** `rm -rf dist && npm run build` reddened eight Playwright tests
    because the e2e `webServer` needs the `ALLOW_DEV_AUTH=true` build CI makes for it, and my production
    build had correctly stripped the dev provider. The browser console line named the cause exactly
    (`AUTH_MODE=dev is not permitted in this production build`) while the assertion did not — rebuild the
    way the harness does and read `.github/workflows/` for which flags that is.

85. **"X is not installed" is not "X cannot be installed", and recording a gate step as unavailable is a
    claim to test once.** `make helm-validate` was the one gate step I never ran locally, on the strength
    of "`kubeconform` not installed"; I deferred it to CI eight times and said so each time as a property
    of the sandbox. It took one `curl` and a `cp`, and the target then passed on the first run — 31 and 35
    manifests valid — meaning every chart change that session was verified only by a remote job I could
    have reproduced in a minute. `CLAUDE.md` carries exactly this argument about Docker ("that message
    describes a default, not a limit") and I re-derived the mistake against a different tool in the same
    session, having read that paragraph at session start. The same shape inside the tree: I shipped a
    rotational profile saying no barrier had been computed against real xTB and that closing it "needs the
    live lane" — `tblite` *is* the GFN2 Hamiltonian, ships as a wheel, and was **already installed** in
    the sibling venv. One `import tblite` would have settled it; running it took twenty minutes and found
    two real defects on the flagship case. **An "it needs X" in my own summary is a claim about the
    environment, and claims about the environment are cheap to test** — check the import, the binary, the
    port, before writing it down. **Before recording a gate step as unavailable, try to make it available
    once.**

86. **An instrument that returns the same number for both arms may be blind rather than agreeing, and a
    control keyed on a condition this tree cannot produce is the appearance of a control.**
    `SSLContext.get_ca_certs()` does not report a `capath` at all, so my four-row trust-store table read
    `0 | 0` and I recorded agreement — while an ambient `SSL_CERT_DIR` was silently widening a configured
    CA pin. **When a comparison's two sides agree at zero, prove the instrument can produce a non-zero
    before believing it**; a real handshake was one page of code and found the defect the table was
    written to rule out. The other half of the same family is a control that cannot fire:
    `make prose-validate` refused a `PromptBlock(absent_unless={"adiabatic_temperature_rise", …})` and was
    right, because **this tree declares no `thermalsafety` bundle**, so the block would have been dropped
    from no deployment, ever — `reject_widening` and `map_to_hpc_identity` in a prompt. What shipped was
    the honest half: the denial narrowed to what is true in every lane, and the refutable consequence
    dropped rather than keyed. **Before keying anything on a tool name, check that this tree can bind it** —
    writing a capability in one repository does not make it reachable from another, that takes a manifest,
    and under-claiming a limit is the safe direction. **And a subagent's ranked root causes are
    hypotheses:** three confident, well-formatted candidates for one UI defect all rested on the premise
    that zustand's `persist` suppresses subscriber notification for non-persisted fields — it does not,
    `partialize` decides what is *written*, not who is notified, and the real cause was two layers away
    (`http-proxy` emits `proxyRes` before copying headers, so an early `flushHeaders()` voids the copy).
    Take a subagent's *facts* — the version, the selector expressions, the reducer source — and check any
    mechanism claim against how the library behaves before building on it.

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

87. **When a finding is about a boundary, read both sides of the wire before writing the
    recommendation.** A published review of how information moves between agentic steps was right about
    the shape and wrong on five specifics, four of them the same error — reasoning about a contract from
    one end. I named a tool to accept a geometry that the calculation server does not serve
    (`compute_properties_at` exists, `compute_fukui_at` does not); I used `QmJobSpec` as the worked
    example when its geometry contract lived in a Nextflow pipeline that *silently ignores* a param no
    process consumes; I called `sample_conformers` the worst payload when `find_calculations` is 28×
    worse (~831,000 tokens against ~7,400) and I had not measured it; and I missed that
    `calculation_key` already returns `structure_id` and the client drops it, which was the fact that
    made the whole fix cheap. The companion repos are two minutes away (`add_repo` + `git clone`).
    **Measure the tool you did not think of** — "which surfaces return a stored payload of unbounded
    size?" would have caught it; "how big is this result?" did not. **And when a key is derived on the
    other side of a wire, ask what it names**: three migration defects were all the server deriving the
    cache key while the client stores the payload, so the client cannot see which arguments the key
    covers — `optimize_geometry` and `relax_structure` share one key and return different payloads, a
    Fukui key does not name the mode, and `multiplicity=None` means the opposite on the two sides. None
    raises; they produce a wrong answer, a stale ranking and a misleading refusal. Ask the server for
    the key of every *pair* of calls that could plausibly collide and diff them; any argument the key
    does not name is omitted from the request or re-applied locally after the cache. **A green server
    suite is not evidence that a client can call the server** — twenty-seven route tests each wrote the
    body the *server* expected, so an endpoint its only client could not call stayed green through
    `mypy --strict`, ten validators and 6,406 tests; where a route's only caller is a companion repo,
    read that call site and test the literal body it sends, and verify a cross-repository shape by
    dumping the producer's own schema (`model_json_schema()`) rather than re-reading the consumer.

88. **A second producer of an existing event, record or metric changes the meaning of every reader,
    and a field with no reader is a lie with a schema.** `ToolFailedEvent` had one producer; adding a
    second made `evals/live` score a clean turn `failed_loudly=True`, `turn_costs` book failures against
    successful calls, and `Chemclaw3_ui` paint two red rows above a good answer — and two of the three
    broken readers are named in the event's own docstring. Not "does my new case work" but **"what does
    the *old* reader make of my new case"**, one grep for the type name across `src/` and the siblings,
    one sentence per hit. The mirror: `required_roles` on the sink manifest was documented as an access
    control and read by nothing, written while citing the ADR about an entitlement defaulting to `[]`;
    `AwaitingBrief` carried `subject`, `kind` and `due_at` under "the four fields anything renders",
    where the badge reads `.length`. **For a new state field, grep the readers before writing the
    docstring, and let the grep write it.** Worse than a dead field is one a person is *promised*:
    `POST /protocols/{id}/status` validated a `reason` to 2,000 characters and dropped it, while the UI
    labelled the box "recorded with the move", disabled every button until it was filled in, and
    confirmed it was "recorded against you with the reason you wrote". Trace every request field to a
    write, and grep the consumer repository for the ones a person types.

89. **A new column's default is an assertion about every row that already exists, and a migration is
    immutable from the commit that introduces it.** `NOT NULL DEFAULT 0` on `retrieval_calls`
    backfilled the whole history of `turn_costs` with "answered without consulting the record" — the
    most interesting value the column can take — in a migration whose own next paragraph argued exactly
    this about the column beside it. **Ask what a query for the most interesting value would return on
    the day it ships; if that answer includes rows nobody measured, it is nullable.** And "nobody has
    applied it yet" is not a fact the repository can hold: I edited migration 082 in place because it
    was unmerged, and my own dev database had already applied it. Who has applied a migration is
    unknowable from inside the tree, so the rule is mechanical — a correction is a new file. (Rule 22's
    converse also still bites: a *reconciliation* such as a privilege grant set must be re-applied on
    every deploy, so filing it as a numbered migration, applied once and tracked by checksum, is the
    wrong semantics.)

90. **An extraction is a diff against the original, not a paraphrase of it — and ask what a literal
    was *for* before replacing it with a variable.** Another branch extracted `sleep` from the module
    whose version had the abort-listener cleanup and shipped the version without it, under a docstring
    saying it took the one "that got it right"; the new home gets the test the old one had, on the
    function rather than through its caller. Replacing `backoff(6, …)` with `backoff(attempt, …)` to
    make a silent branch report was correct *and* silently cut the concurrent-stream cap's first wait
    from 15–30 s to 1–2 s, because the literal was a **saturation point** and not a magic number — it
    surfaced as a ~50% flake, not a failure. If a literal is a saturation point, a ceiling or a floor,
    keep it as `max`/`min` around the variable. **And when a docstring cites another module as the
    authority for a decision, open it and diff the predicate:** `errorFromStatus` split two 429s on
    whether `Retry-After` was *present* while `useJobStreams` split them on whether it *parsed*, under
    a docstring pointing at the other file. Two files that say they do "the same thing for the same
    reason" will not.

91. **Verify a merge against a machine-computed merge of its own parents.** My merge commit deleted
    one line from `connectors/registry.py` — the whole subject of the branch's first fix, present in
    both parents *and* the merge base — because four review agents were mutating `src/` while I resolved
    with `git add -A`; checking the result against my *pre-merge HEAD* is blind by construction to any
    file the merge legitimately touches. The check that finds this class is `git diff $(git merge-tree
    --write-tree <parent1> <parent2>) <merge>^{tree}`: it names every file where the committed merge
    differs from a clean merge of the same two parents, so a deliberate resolution and an accidental
    capture appear side by side and have to be told apart one at a time. Mine showed three files; two
    were resolutions and one was the accident. **Resolve a merge, then grep for the markers** —
    `docs/decisions/README.md` reached a commit with `<<<<<<< HEAD` in it and eight ADR rows dropped,
    with `make lint` and `make type` both green because the file is Markdown. **And a merge resolver
    that reconciles two append-only registers by key must treat a *changed* row as a conflict, not a
    duplicate**: mine keyed the ADR ledger by id and did `rows[id] = line` ours-then-theirs, so `main`
    won every collision and silently discarded a correction the branch had made. It reported a clean
    merge. **A clean auto-merge is not evidence the tree is unchanged** either: merging a stale branch
    whose PR had been squashed re-added a `BACKLOG.md` row I had deleted, with no conflict, because
    from the merge base's view that branch *adds* the row — after such a merge, diff the result against
    your own pre-merge HEAD and assert zero files differ.

92. **Deleting a subsystem leaves readers whose input nothing produces, and they pass their own
    tests.** Five reviews of the PR-gate deletion found nine defects, four the same shape I had not
    looked for: `operations.authorship` and the evidence pack still read `note_proposals`, a table the
    deletion left standing and emptied of any producer, returning `0` and `[]` — which is what an idle
    system returns, and worse than a crash because a plausible zero is indistinguishable from a quiet
    afternoon. I had grepped for the removed *thing* and fixed every hit; that finds the writers, while
    a reader names a table, a column or a concept and survives the grep. **Enumerate the tables,
    columns, metrics and config keys the deleted code was the only writer of, then find every reader of
    each and ask: who writes what this reads now?** Three more traps in the same operation. **A guard
    named after the thing it guards disappears with it, silently:** `assert not hasattr(qm_knowledge,
    "write_knowledge_node")` becomes vacuously true when the module goes — no failure, no signal, and a
    control that still *reads* like one in review; rewrite it over the whole set (an AST walk, a
    registry scan) before deleting. **An invariant usually outlives the thing it was written against:**
    the parent-ceiling validator was phrased against the DFT poll's 24 h budget, and the rule — a
    workflow's execution timeout must exceed the longest activity under it, or the retry budget is
    unreachable and the error names neither setting — applies verbatim to the longest CREST search;
    restating a rule without naming its subject is the test, and the same question re-derived a default
    (`connector_job_timeout_seconds` was 90,000 s because of that poll, and every other job had
    inherited a ceiling sized for a tier that never ran). **And a cache outlives the code that filled
    it:** `calculation_results` is never pruned, so the `dft` projector had to stay while the
    `QMJobResult` entry four lines away had to go. **Finally, a deletion invalidates arguments made by
    the code that stays** — the sidecar's `rsync --delete` was safe for exactly as long as the writer
    committed elsewhere, and moving the writer into that tree turned it into silent permanent data
    loss, with nothing in either file mentioning the other. When a component moves, re-read what was
    safe *because* of where it used to be.

93. **Re-read the base branch before writing the fix, not before pushing it.** An ADR contradicting my
    helm rationale merged while I was working and I never re-read `main`; then three PRs landed while
    four reviewers ran, one of them rebuilding the judge on the provider seam, so a TLS fix I had
    written, tested and described in an ADR was obsolete before it merged and the merge conflict was
    the only thing that told me. **A claim is checkable against `main`, not against the branch point** —
    specifically the claims my prose makes about the same subsystem — and whenever a review takes long
    enough for the base to move, that re-read belongs at the start. **A rejected alternative's argument
    also survives the rejection being overturned:** when the local-skills tier came back, the objection
    I had raised against it (per-user skills fragment answers across chemists with nothing recording
    why) was still true, and deleting it with the rejection would have lost the reason the design needs
    an inspectability invariant — re-file a losing argument as a stated cost or as a requirement it
    generates, never as though it had been wrong. **And an absolute-sounding qualifier in a one-line
    instruction is a question, not a spec:** "things that change agent behaviour like skills only after
    human review" has two grammatical readings differing in exactly one property — whether an
    unreviewed skill may touch its own author's turns — and I wrote a whole ADR section on the wrong
    one. When a short instruction turns on a quantifier and I can state the distinguishing property in
    a single sentence, I can ask instead.
