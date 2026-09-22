# Wave 5 — the cheapest check on a gate is whether it starts

Wave 5 began as a triage of every remaining row, because waves 1–4 closed the self-contained defects
and six of eighteen worked rows turned out to be wrong about something. The triage is the first
deliverable; the two rows worked below are the second.

## Triage of all 43 open rows

| verdict | rows | what it means |
|---|---|---|
| **Declined** | 7 | carries an explicit decline with a trigger; nothing to do until the trigger fires |
| **Blocked** | 8 | needs a real broker, cluster, tenant or companion-repo change this session cannot reach |
| **Open** | 28 | actionable here |

Five spot-checks against `HEAD`, four of which corrected the row:

- `STANDARDIZATION_VERSION` is **std9**, not what its row says.
- `[tool.mutmut]` had **14** source paths, not the number cited.
- **7** bundle `SKILL.md` files ship; a row says 3.
- One row's anchor, `tests/test_sibling_manifest_agreement.py::_DISPATCHERS`, exists nowhere in the
  tree.
- The fifth held exactly as written.

## R1 — "four refusal gates are outside the mutation backstop"

The row's instruction was to measure the run's cost before extending it. **The run did not run.**

- [x] Measured: `make mutants` exits non-zero before a single mutant is tested. **27 s** locally —
      on the 15-path config, which the log's own "15 files mutated" says; the `origin/main` 14-path
      config reaches the identical failure and I recorded no wall clock for it. The figure that
      belongs to `main` is CI's: **29 s** in the scheduled workflow's own mutate step.
- [x] **Blocker 1** — two derived guards collided. `api/runner_trace.py` is in `source_paths`, so
      mutmut rewrites `ToolCallTrace` inside `mutants/` with 45 generated names
      (`xǁToolCallTraceǁissued__mutmut_3`, …); `tests/test_runner.py`'s offered-methods guard walks
      `vars(ToolCallTrace)`, skips names starting with `_`, and requires each survivor to be called
      from `src/chemclaw/api` — which nothing could. Fixed with `"__mutmut_" not in name` in the
      test rather than an exclusion in the config, because those are not methods the class *offers*.
- [x] **Blocker 2 — the one that mattered.** With blocker 1 fixed the run reaches the clean-test
      baseline and dies at "48 of 24 concurrent appends survived", with 48 matching rows in
      **`public.audit_events`**. `mutmut.__main__.PytestRunner.execute_pytest` calls `pytest.main()`
      *in-process* — stats, baseline, then once per mutant — while
      `conftest.py::isolated_postgres_schema` drops `tests.pg.TEST_SCHEMA` at session end and
      recreates it empty at the next session's start, and `TEST_SCHEMA` is a module constant. So
      `_MIGRATED` kept an unchanged key for a vanished schema, `migrated_db_or_skip` applied nothing,
      and every unqualified name resolved through the search_path to `public`.
      **Reproduced in ≈4.4 s of pytest time**: two `pytest.main()` calls on one test in one process
      — 4.04 s for session 1, isolated and green, and 0.38 s for session 2, which appends 24 rows to
      `public.audit_events`. (0.37 s appears in no log at all; it was the second session's duration,
      misremembered, quoted as the whole reproduction.) A mutant run selects 16 test files, of which
      **2** contain a `TRUNCATE`/`DELETE FROM` and only one runs a (scoped) delete, so what leaked
      here was audit rows — repo-wide it is 52 of 417 files, not "a third", and the `note_index`
      truncations are in two files a mutation run never selects.
      Fixed in `drop_test_schema`, which now discards the memo entries naming the schema it just
      dropped — the statement that falsifies the memo is the only place a second dropper cannot
      forget. `tests/test_isolation_across_sessions.py` drives two real in-process sessions; verified
      it reds with the fix stashed (session 1 passes, session 2 fails) and greens with it.
      The 96 residue rows my measurements left in `public.audit_events` were deleted.
- [x] My first diagnosis of blocker 2 was wrong, and so was my first correction of it. I read the
      earlier run's `test_knowledge` pytest-timeout as the blocker and wrote it into the row; the next
      attempt died elsewhere, so I rewrote the row saying "with `PYTEST_TIMEOUT_SCALE=4` that test
      passes" — which I never observed. It does not: the scale applied (720 s) and it timed out anyway.
      The second attempt simply stopped earlier in the same file order under `-x`. **A failure you did
      not reach is not a failure you disproved.**
- [x] **Blocker 3, root-caused and fixed (Wave 6).** With both above fixed, the clean baseline gets
      through 271 tests in **12m33s** and hangs in
      `test_concurrent_writes_serialize_and_both_notes_land`. Not the mutated code — it passes in
      3.56 s alone against `mutants/src`. It is the second in-process session again:
      `kg/git_writer._WRITE_LOCK` is a module-level `asyncio.Lock`, `Lock.acquire` resolves its loop
      only on the *contended* path, so the lock binds to the first loop that races on it and a second
      `asyncio.run` raises `RuntimeError: ... is bound to a different event loop` **from the waiter**,
      leaving the lock permanently `[locked]` and `asyncio.run`'s shutdown cancelling a holder that
      never finishes. Reproduced in milliseconds with no pytest at all. `core/temporal_client`
      `._CONNECT_LOCK` has the same defect. Both are now `core/aio.LoopLocalLock`, resolved per
      running loop — see the Wave 6 section below.
- [x] One of the four gates added, not four, honouring the row's instruction now that a measurement
      is possible: `agent/spend_cap.py`, the smallest of the four **by source length** (309 lines
      against 333 / 367 / 671). Lines are a proxy for mutant count and the comment says so — my
      first version of it called `plan_gate.py` the smallest, which is the largest of the four.
- [x] ADR (`D-2026-09-22-a-mutation-backstop-that-cannot-start`) and the row **rewritten, not
      deleted**: the measurement it asks for still needs one completed run.
- [ ] A completed run and its cost. Running in the background; the row carries what is left, and a
      first figure is a 15-path *baseline* rather than this addition's marginal cost.

## R2 — "a parse reached outside the isolate child is bounded by nothing"

The row named two honest options and asked for one. Verified at `HEAD` first:
`attachments.parse_attachment` calls `parse_document` with no ceiling anywhere on the path, and
`isolate._at_ceiling` returns `False` whenever `ceiling is None` — so an allocation failure inside a
C parser and a genuinely malformed file are the *same observation*, reported in the parser's own
words (`unknown error (<string>, line 0)`) as an accusation against a document that may be fine.

- [x] **The forkserver is declined**, with the trigger written. `parse_attachment`'s own docstring
      already argues the in-process choice is right for its two callers — `backfill_corpus` is an
      operator command where a slow parse costs the operator their own wait, and the format tests
      assert what each parser extracts. Neither is a shared replica, which is why
      `parse_attachment_isolated` exists for the upload route.
- [x] `UnclassifiedParseError`, a `DocumentParseError` subclass, marks **the one** broad `except` arm
      in `parse_document`; `read_without_a_ceiling(cause)` keeps the parser's message verbatim and adds
      what an unbounded path cannot establish. **The distinction is in the type, not the string** — a
      caller cannot sniff an allocation failure out of a parser's message, which is why `_at_ceiling`
      measures `VmData` instead, and the wording keeps the type rather than rewrapping as the base
      class, which would erase the distinction it is keyed on.
- [x] **I wrote "the two broad arms" and a review measured it false**, in five places and in the
      implementation. The second site was `_refuse_a_bomb`'s `except zipfile.BadZipFile` — narrow and
      *classified*, "this is not a zip archive", and not an allocation failure since `zipfile` raises
      `MemoryError` when it runs out. So "File is not a zip file" arrived followed by a paragraph
      about memory not being established: the caveat on the one population with a definite verdict,
      which is the exact failure this row exists to fix, committed inside the fix.
- [x] A classified refusal passes through untouched, and **all four** named populations are now driven
      — unsupported format, not-a-zip container, over-expanding archive, scanned PDF. Naming three and
      asserting one is how the fourth got buried.
- [x] By inheritance, so every existing handler is unchanged. One caller does read the class *name*
      (`sync.py` counts by `type(exc).__name__`), and `tests/test_document_share.py` pinned the
      literal `DocumentParseError` there — so the more precise type reddened a test that was green on
      main. The assertion is on the suffix now; the operator's line gets strictly more specific.
- [x] The AST guard **rewritten to derive `except` handlers rather than count `raise` statements**. The
      first version asserted "two raises" and passes unchanged when a third broad arm is added that
      raises the base class — measured — so it held neither direction. It now requires every broad
      handler to raise this type and no narrow one to, and the second assertion is what would have
      caught the defect above.
- [x] ADR (`D-2026-09-22-an-unbounded-parse-may-not-blame-the-document`) and the row deleted, 43 → 42.

## Verification

- [x] `make lint` (ruff lint + format), `make type` (962 files) and `make prose-validate` green.
- [x] `tests/test_decision_log.py` 20 passed · `tests/test_isolation_across_sessions.py` 2 passed,
      and red with the fix stashed (session 1 passes, session 2 fails).
- [x] **Two fresh-context subagent reviews, read-only.** Between them: one red test this commit caused,
      the one-vs-two broad arms error and the buried classified refusal behind it, an AST guard that
      did not hold its advertised property, four copies of a false "no CI job runs it", a misquoted
      27 s, an invented 0.37 s, an unsupported "27 minutes", a 3x-overstated blast radius, a
      `_forget_migrations_in` docstring promising more than substring matching can give, a "recorded
      below" with nothing below it, and `spend_cap.py` added as a mutation source with its own test
      file outside the selection (70% vs 96% coverage, ~18 statements of unkillable mutants). All
      fixed above.
- [ ] `make lint type` and the full serial suite over the review fixes.
- [ ] PR, merge on green CI, delete the branch.

## Review

Both rows were wrong about something, which is now eight of twenty worked rows found stale or
misstated — and in both cases the error made the defect look smaller than it was.

**R1's row treated the backstop as a working control that was merely too narrow.** It was empty.
Every module in `source_paths`, `agent/authz.py` included, had been outside the backstop for as long
as the collision had existed. The row's cited cost — "the run is hours long" — describes a
full-repository run in a comment the row was reading, not this list, whose last completed run on
record is 825 mutants in 2m57s from 2026-08-27, when it held seven paths.

**And I wrote four times that no CI job runs it, which a review falsified.**
`.github/workflows/mutants.yml` has run it weekly on `main` since 2026-08-28, gates on the kill rate,
and files an issue on failure. All four scheduled runs failed; issue #295 has been open since the
first, with three comments. So the control reported its own death every Monday for a month into the
tracker this repository works its backlog in — and the backlog row about it was written as though the
control were healthy. A signal nobody reads is not a missing signal.

**The lesson is still that the cheapest possible check on a gate is whether it starts**, and nobody
had spent 29 seconds on it. Behind that was a worse finding the row could not have predicted: from its second
in-process session onward, the mutation run was operating destructively on the developer's own
database. Three properties of the suite's isolation — the session fixture, the migration memo and
`TEST_SCHEMA`'s uuid4 suffix — were each written against "one run is one process", true of `make
test`, of CI and of every xdist worker, and false of `pytest.main()`. None was wrong about a
process; the assumption was simply never named, so nothing checked it.

**R2's row was right about the defect and right that it needed a decision** — the only row so far
this session whose framing survived contact. Its two options were both honest, and what tipped it is
that the argument for the in-process choice was already written in the function's own docstring and
still holds. So the asymmetry is the decision: a chemist uploading through the API gets a bounded
parse and a refusal that distinguishes the causes; an operator running a backfill gets an unbounded
parse and a refusal that says so.

Lessons 126–132 added. 126 is the one worth repeating: I wrote "`plan_gate.py` is the smallest of
the four by mutant count" when it is the **largest** by source length and I had measured no mutant
counts at all — a comparative claim, inside the config comment justifying the choice, with no
measurement behind either half of it.

## Wave 6 — a lock built at import belongs to one event loop

Wave 5's third blocker, which is product code rather than test scaffolding, so it gets its own
commit and its own ADR (`D-2026-09-22-a-lock-built-at-import-belongs-to-one-event-loop`).

- [x] `asyncio.Lock.acquire` resolves its loop **only on the contended path** — the fast path sets
      `_locked` and returns before `_get_loop()`. Measured both arms: two sequential `asyncio.run`
      calls raise nothing when the lock is never contended, and `RuntimeError: ... is bound to a
      different event loop` when it is. That is why two of these survived in `src/` behind comments
      that had considered the multi-loop case: the uncontended path is what they pictured.
- [x] The failure is a **hang, not an error**. The *waiter* raises while the holder keeps the lock,
      so `asyncio.run`'s shutdown cannot finish cancelling the holder and the lock stays `[locked]`
      for everyone after. Driven on the real writer: 3.4 s for the first `asyncio.run`, no return
      from the second.
- [x] `core/aio.LoopLocalLock` — one `asyncio.Lock` per running loop, an async context manager so
      both call sites read unchanged. Two real callers, which is what the Rule of Three asks.
- [x] **A `WeakKeyDictionary` cannot implement it, and one arm of the measurement would have hidden
      that.** A contended `asyncio.Lock` stores `_loop`, its own key, so the entry keeps itself
      alive: 0 of 3 entries survive when uncontended, **3 of 3** when contended. A plain dict that
      discards closed loops when read holds the same property by a checkable route.
- [x] The weakening is stated at both declarations: two *simultaneous* loops no longer serialize,
      which `git_writer`'s exclusive `flock` and `temporal_client`'s "a second channel is a cost,
      not a fault" already cover. Sequential loops keep the old semantics.
- [x] A rule over the tree, not a list of two modules:
      `test_no_asyncio_primitive_is_built_at_import_time_in_src` walks every module in `src/` for a
      loop-bound primitive constructed at import, class bodies included.
- [x] The regression pinned where it bit —
      `test_two_sequential_event_loops_can_both_write_concurrently` drives two real loops through
      `GitNoteWriter`, verified to **time out** with the fix stashed and pass with it. It is in the
      mutation selection, so the run exercises it.
- [x] `make lint`, `make type` (964 files), `make prose-validate` green; `test_loop_local_locks` 5,
      `test_knowledge` 50, `test_temporal_client` and the six repo-hygiene guards 1174 passed.
- [ ] `make mutants` re-run to see whether it now completes, and the full serial suite.

## The suite, twice, and a test that cannot run in this sandbox

- [x] Run 1: **10541 passed, 7 skipped, 3 failed** (23:26). Two were guards this session's own prose
      tripped — `core/aio.py` citing `_locked` while defining `locked`, and "issue #295" read as a
      pointer to rule 295. Both guards were right; both sentences rephrased.
- [x] Run 2: **10543 passed, 7 skipped, 1 failed** (23:12), and the one failure was a *different arm*
      of the same test. Run 1 failed its `in_flight` assertion; run 2 failed its budget precondition.
      Fixing the arm that fired told me nothing about the other one.
- [x] `in_flight` was a real race: the slot comes back when the worker thread does — the production
      comment says so — and that thread is still killing the child, measured at 0.058 s from the
      test's own log, while the assertion allowed one loop turn. Bounded at 5 s, which is two orders
      of magnitude from the wedge it regresses against (`in_flight` still at 2 after five seconds).
- [x] The budget precondition is a property of **this box**. Five samples: fork round trip **0.165 s
      median** against CI's **0.030 s**, parse 0.205 s against CI's 0.258 s — so the parse is healthy
      and the required ratio of 4 measures 1.24. The guard was right to refuse and wrong to refuse by
      failing. It now skips when the *fork* is the outlier and still fails when the *parse* got
      cheap, with both arms driven on faked clocks.
- [x] `tests/conftest.py::_report_slow_fork_skips` — the fourth reporter beside Postgres, Temporal
      and helm, so a run that skips these says what it is therefore not evidence about. Verified
      firing: "3 tests were skipped because creating a process costs more here than the deadline
      they derive".
- [ ] A third full run, then the PR.
