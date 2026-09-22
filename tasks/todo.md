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

- [x] Measured on `origin/main` with no other change: `make mutants` exits non-zero in **27 s**,
      before a single mutant is tested. Verified by reverting `pyproject.toml` alone.
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
      **Reproduced in 0.37 s**: two `pytest.main()` calls on one test in one process — session 1
      isolated and green, session 2 appending 24 rows to `public.audit_events`. The count assertion
      is the harmless end; the same suite `TRUNCATE`s `note_index`.
      Fixed in `drop_test_schema`, which now discards the memo entries naming the schema it just
      dropped — the statement that falsifies the memo is the only place a second dropper cannot
      forget. `tests/test_isolation_across_sessions.py` drives two real in-process sessions; verified
      it reds with the fix stashed (session 1 passes, session 2 fails) and greens with it.
      The 96 residue rows my measurements left in `public.audit_events` were deleted.
- [x] My first diagnosis of blocker 2 was wrong and measuring is what corrected it. I read the
      earlier run's `test_knowledge` pytest-timeout as the blocker and wrote it into the row; with
      `PYTEST_TIMEOUT_SCALE=4` that test passes and the run dies somewhere else entirely.
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
- [x] `UnclassifiedParseError`, a `DocumentParseError` subclass, marks the two broad `except` arms in
      `parse_document`; `read_without_a_ceiling(name, cause)` keeps the parser's message verbatim and
      adds what an unbounded path cannot establish. **The distinction is in the type, not the string**
      — a caller cannot sniff an allocation failure out of a parser's message, which is why
      `_at_ceiling` measures `VmData` instead.
- [x] A classified refusal passes through untouched: unsupported format, over-expanding archive,
      scanned PDF. Burying those under a memory caveat is the same failure pointed the other way.
- [x] By inheritance, so every existing handler is unchanged; verified nothing in `src/` or `tests/`
      tests the exact class rather than catching it.
- [x] Three tests: the caveat present on an unclassified population, **absent** on a classified one,
      and the set of broad arms derived from `parse.py`'s own AST (== 2), so a third added later is
      either covered or red.
- [x] ADR (`D-2026-09-22-an-unbounded-parse-may-not-blame-the-document`) and the row deleted, 43 → 42.

## Verification

- [x] `make lint` (ruff lint + format) and `make prose-validate` green.
- [x] `tests/test_parse_isolation.py` 17 passed · `tests/test_decision_log.py` 20 passed ·
      `tests/test_isolation_across_sessions.py` 2 passed, and red with the fix stashed.
- [ ] `make type` and the full serial suite over the wave's changes.
- [ ] Fresh-context subagent review, read-only, before the PR.
- [ ] PR, merge on green CI, delete the branch.

## Review

Both rows were wrong about something, which is now eight of twenty worked rows found stale or
misstated — and in both cases the error made the defect look smaller than it was.

**R1's row treated the backstop as a working control that was merely too narrow.** It was empty.
Every module in `source_paths`, `agent/authz.py` included, had been outside the backstop for as long
as the collision had existed, and nothing said so: the target exits non-zero, which only a person
running it deliberately would see, and no CI job runs it. The row's cited cost — "the run is hours
long" — describes a full-repository run in a comment the row was reading, not the 14-path run, which
had never completed at all.

**The lesson is the cheapest possible check on a gate is whether it starts**, and nobody had spent
27 seconds on it. Behind that was a worse finding the row could not have predicted: from its second
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

Lessons 126–128 added. 126 is the one worth repeating: I wrote "`plan_gate.py` is the smallest of
the four by mutant count" when it is the **largest** by source length and I had measured no mutant
counts at all — a comparative claim, inside the config comment justifying the choice, with no
measurement behind either half of it.
