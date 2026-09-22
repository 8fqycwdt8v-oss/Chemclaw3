# D-2026-09-22-a-mutation-backstop-that-cannot-start — `make mutants` covered nothing

**Status:** accepted · **Date:** 2026-09-22 · Supersedes nothing. Found while working the `BACKLOG.md`
row *"Four first-party refusal gates are outside the weekly mutation backstop"*, whose instruction was
to measure the run's cost before extending it. The run does not run.

## Context

`[tool.mutmut]` names 14 source paths and a long comment argues which modules earn a place among them.
The backlog row asking for four more — `agent/plan_gate.py`, `agent/skill_backend.py`,
`agent/spend_cap.py`, `agent/loop_cap.py` — says the list is "short on purpose", because "the comment
above it records that the run is hours long", and asks for each addition's runtime to be measured
rather than assumed.

**Measured: `make mutants` fails in 27 seconds, before a single mutant is tested.** Driven on
`origin/main` with no other change:

```
done in 4353ms (15 files mutated, 0 ignored, 0 unmodified)
Running stats ... F
failed to collect stats. runner returned 1
```

The failing test is `tests/test_runner.py::test_every_method_the_trace_offers_is_one_the_shipped_turn_calls`,
and the reason is a collision between two derived guards that are each correct on their own:

- `api/runner_trace.py` is in `source_paths`, so mutmut rewrites it inside `mutants/` — `ToolCallTrace`
  there carries 45 generated names beside its real methods (`xǁToolCallTraceǁissued__mutmut_3`, …).
- That test walks `vars(ToolCallTrace)`, filters names starting with `_`, and requires each survivor to
  be called from some module under `src/chemclaw/api`. mutmut's names do not start with `_`, and
  nothing calls them, because nothing could.

mutmut's *stats* phase runs the test suite unmutated inside `mutants/` to learn which tests touch which
code. So the failure is not a surviving mutant or a flaky test: it is the run refusing to begin.

**Behind it was a second failure, and that one was not merely a stopped run.** With the guard fixed the
run reaches the clean-test baseline and dies in `tests/test_audit_store.py::test_concurrent_appends_all_arrive`
at "48 of 24 concurrent appends survived" — twice the expected count, with 48 matching rows sitting in
**`public.audit_events`**, the dev database's own audit trail. `mutmut.__main__.PytestRunner.execute_pytest`
calls `pytest.main()` *in-process*: stats, then the clean baseline, then once per mutant, all in one
Python process. `tests/conftest.py::isolated_postgres_schema` is session-scoped, so it drops
`tests.pg.TEST_SCHEMA` when a session ends and recreates it empty when the next begins — while
`TEST_SCHEMA` is a module constant and keeps its name. `tests.pg._MIGRATED` keyed on that unchanged
name, so from the second session onward `migrated_db_or_skip` applied nothing, every unqualified name
resolved through the search_path's second entry, and the suite ran against `public`.

Reduced to two `pytest.main()` calls on that one test in one process, it reproduces in **0.37 s**:
session 1 isolated and green, session 2 appending 24 rows to `public.audit_events`. The count assertion
is the harmless end of it — the same suite truncates `note_index` and issues `DELETE`s across a third
of its files.

That is a defect, so it is a commit and a test rather than a decision: `drop_test_schema` now discards
the memo entries naming the schema it just dropped, because the statement that makes the memo false is
the only place a second dropper cannot forget. `tests/test_isolation_across_sessions.py` drives two real
in-process sessions and reds without it.

**The consequence is the finding.** The row treats the backstop as a working control that is merely
too narrow — four refusal gates outside a list. It is not narrow; it is **empty**. Every module in
`source_paths`, including `agent/authz.py`, has been outside the backstop for as long as this has been
true, and nothing said so: the target exits non-zero, which a person running it deliberately would see,
and no CI job runs it.

## Decision

**Filter mutmut's scaffolding out of that derived guard, and treat "the run starts" as the thing to fix
before the row's measurement means anything.**

The one-line change is in the test rather than in `[tool.mutmut]`: `"__mutmut_" not in name`. The
argument is that those are not methods the class *offers* — they are a harness's rewriting of the ones
it does — and the guard's subject is the shipped surface. Excluding the test from the mutant run's test
selection instead would hide it, and it is a test this repository wants running inside `mutants/` as
much as outside.

**One of the four gates is added, not four**, honouring the row's instruction now that a measurement is
possible. `agent/spend_cap.py` is first because it is the smallest of the four by source length (309
lines against `loop_cap.py`'s 333, `skill_backend.py`'s 367 and `plan_gate.py`'s 671), so it buys the
cheapest available answer to what one of these costs. Lines are a proxy for mutant count and not a
measurement of it, which is stated where it is used.

## Consequences

**The row's premise is corrected rather than carried forward.** "The run is hours long" describes a
*full-repository* run, in the comment the row cites; what the 14-path run costs was unknown, because it
had never completed. Whatever this addition's measured cost turns out to be, it is now measured against
a baseline that exists.

**A test harness that reuses one process is a second environment the suite has to hold in.** The
isolation fixture, the migration memo and `TEST_SCHEMA`'s uuid4 suffix were each written against
"one run is one process", which is true of `make test`, of CI and of every xdist worker — and false
of `pytest.main()`. Nothing in the suite was wrong about a process; the assumption was simply never
named, so nothing checked it. `tests/test_isolation_across_sessions.py` names it now, and
`tests/pg.py`'s own comments say which of its properties hold per process rather than per session.

**Two derived guards collided, and that is the general lesson.** Both are the shape this repository
prefers — a rule over the tree rather than a list somebody maintains — and each was written without the
other in view. A guard that walks a class's members is a guard that sees whatever rewrote that class;
a guard that rewrites the tree is a guard that must survive the tree's own guards. Neither is wrong,
and the collision was invisible because the only signal was a non-zero exit from a target nobody runs
on a schedule.

**Not fixed here: nothing runs this.** `make mutants` is absent from the `ci` target — deliberately,
since the run is long — so its exit code reaches a person only when a person asks. That is what let a
27-second failure stand. Making it a scheduled job is a real decision with a real cost (a runner for
hours, weekly) and is not this change's to take.

**Revisit when:** `make mutants` fails to start again, or the run's measured duration is known and a
weekly job for it is worth costing. The two tests that fail the next time either of these recurs are
`tests/test_runner.py`'s offered-methods guard and `tests/test_isolation_across_sessions.py`; each now
names its hazard in its own docstring, so the next reader of a `mutants/` failure has the mechanism in
front of them rather than 45 generated names and no explanation.
