# D-2026-09-22-a-mutation-backstop-that-cannot-start — `make mutants` covered nothing

**Status:** accepted · **Date:** 2026-09-22 · Supersedes nothing. Found while working the `BACKLOG.md`
row *"Four first-party refusal gates are outside the weekly mutation backstop"*, whose instruction was
to measure the run's cost before extending it. The run did not run.

## Context

`[tool.mutmut]` names 14 source paths and a long comment argues which modules earn a place among them.
The backlog row asking for four more — `agent/plan_gate.py`, `agent/skill_backend.py`,
`agent/spend_cap.py`, `agent/loop_cap.py` — says the list is "short on purpose", because "the comment
above it records that the run is hours long", and asks for each addition's runtime to be measured
rather than assumed.

**Measured: `make mutants` fails before a single mutant is tested.** Locally, 27 s — and that figure
is from a run whose `[tool.mutmut]` already carried this change's fifteenth path, which the quoted
block says out loud; the `origin/main` 14-path config reaches the identical failure and its wall
clock was never recorded. The figure that belongs to `main` is CI's own: the scheduled `mutants`
workflow's "Mutate the invariant-bearing modules" step, **29 s**, on run 4 of 4.

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

Reduced to two `pytest.main()` calls on that one test in one process, it reproduces in **≈4.4 s of
pytest time** — 4.04 s for session 1, which is isolated and green, and 0.38 s for session 2, which
appends 24 rows to `public.audit_events` and then fails its own count. (Its assertion read *96* of 24
rather than the mutation run's 48, because by then my earlier measurements had left 72 rows there.)

**How much a mutation run could destroy that way is smaller than it first looks, and the number is
worth stating rather than gesturing at.** A mutant run executes
`pytest_add_cli_args_test_selection`, 16 files, not the suite: **2** of those 16 contain a `TRUNCATE`
or a `DELETE FROM` at all, and only one of them runs anything — a `DELETE FROM session_turns WHERE
session_id = %s`, scoped to its own fixture row. Repo-wide the figure is **52 of 417** test files
(12.5%), and the `note_index` truncations live in `test_vector_index.py` and `test_hybrid_rrf.py`,
neither of which a mutation run selects. So what actually leaked into `public` here was audit rows.
The reason to fix it is not the blast radius of this particular harness — it is that the suite's
isolation silently stops holding for *any* caller that reuses the process, and nothing said so.

That is a defect, so it is a commit and a test rather than a decision: `drop_test_schema` now discards
the memo entries naming the schema it just dropped, because the statement that makes the memo false is
the only place a second dropper cannot forget. `tests/test_isolation_across_sessions.py` drives two real
in-process sessions and reds without it.

**The consequence is the finding.** The row treats the backstop as a working control that is merely
too narrow — four refusal gates outside a list. It is not narrow; it is **empty**. Every module in
`source_paths`, including `agent/authz.py`, has been outside the backstop for as long as this has been
true.

**And it was not silent, which is worse.** The first draft of this ADR said no CI job runs the target
and that nothing said so; both are false, and a review measured it.
`.github/workflows/mutants.yml` has run `make mutants` weekly on `main` since 2026-08-28, gates on
the kill rate, and files a GitHub issue when the run reports anything. **All four scheduled runs
failed** — 2026-08-31, 09-07, 09-14, 09-21 — and the issue it filed, #295 *"Weekly mutation run needs
attention"*, has been **open since the first one**, with three comments and a last update of
2026-09-21. So the control reported its own death every Monday for four weeks into the place this
repository works its backlog, and the backlog row about it was meanwhile written as though the
backstop were running and merely narrow.

That is the real defect class here, and it is not a missing signal. A weekly job whose failure files
an issue is exactly the design `mutants.yml`'s own header argues for, against the default of an email
to whoever last touched the cron line. It worked. What failed is that nobody read it — and a control
nobody reads is indistinguishable from the unscheduled one the workflow was created to replace.

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
measurement of it, which is stated where it is used — and the completed run has since priced the real
thing at **29 mutants, ~18 seconds**, so the proxy was pointing the right way and the caution was still
worth writing.

## Consequences

**The row's premise is corrected rather than carried forward.** "The run is hours long" describes a
*full-repository* run, in the comment the row cites, and the only completed run on record before this
one is `mutants.yml`'s own note of **825 mutants in 2m57s warm**, from 2026-08-27, when `source_paths`
held **seven** entries — so "it had never completed" was true of this list and not of the target. It
completes now, at 30m04s for 3640 mutants, which is the figure above and the one the row gets.

**The run completes now, and the measurement changes what the row is about.** 30m04s for 3640
mutants at 1.57 mutations/second, with the three blockers below fixed: 1612 killed, 988 survived,
**1009 reached by no selected test**, 31 timed out, 0 suspicious. `agent/spend_cap.py`'s own share is
29 mutants — 0.8% of the run, about 18 seconds — so "the run is hours long", the sentence the row
declined to widen the list on, priced an addition at ~18 s of a half-hour run.

**And the gate still fails, for a reason worth more than the cost figure.** It scores
`killed / total` = 1612/3640 = **44.3%** against `MUTATION_SCORE_FLOOR: "72.0"`, because `total`
counts the 1009 mutants nothing in `pytest_add_cli_args_test_selection` reaches. That selection is a
hand-kept list of 16 files and was never widened as `source_paths` grew from the seven the floor was
recorded against — 74.7% and 76.8%, in this workflow's own comments — to fifteen. So the weekly job
goes from failing to *start* to failing on a denominator nobody maintained, and `spend_cap.py`
arriving with its own test file outside the selection was one instance of that, caught here only
because a reviewer measured its coverage. The floor is not wrong; it is being read against a set it
was not measured on. `BACKLOG.md` carries the work.

**The third blocker was why it did not complete before.** With the guard fixed and the
isolation escape fixed, the clean-test baseline gets through 271 of its tests in **12m33s** and then
`tests/test_knowledge.py::test_concurrent_writes_serialize_and_both_notes_land` hits pytest-timeout —
at 180 s, and again at 720 s under `PYTEST_TIMEOUT_SCALE=4`, so it is a hang rather than a slow test.
Root-caused to `kg/git_writer._WRITE_LOCK`, a module-level `asyncio.Lock`: `Lock.acquire` resolves its
loop only on the *contended* path, so the lock binds to the first loop that races on it, a second
`pytest.main()` raises `RuntimeError: ... is bound to a different event loop` **from the waiter**, and
the lock is left permanently `[locked]` with its holder mid-cancel. Reduced to two `asyncio.run` calls
with no pytest at all, it reproduces in milliseconds. That is a defect in `src/`, with a sibling in
`core/temporal_client._CONNECT_LOCK`, and it is fixed in its own commit rather than folded in here.

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
and the collision was invisible not because nothing reported it but because what reported it was an
issue in a tracker, three weeks running, that read as a survivor problem rather than a dead harness.

**Not fixed here: the thing that makes a weekly report get read.** The schedule exists and the issue
exists; `make mutants` is still absent from the `ci` target, deliberately, since the run is long.
What is missing is upstream of any workflow file — a scheduled report that files into a tracker is
only a control if somebody works the tracker, and the repository's own convention for that
(`BACKLOG.md`'s "open a GitHub Issue linking back to the row") points the other way, from row to
issue rather than from issue to row. Proposing a mechanism for the reverse is a real decision and is
not this change's to take.

**Revisit when:** `make mutants` fails to start again, or it completes and its measured duration is
known. Three tests fail the next time one of these recurs — `tests/test_runner.py`'s offered-methods
guard, `tests/test_isolation_across_sessions.py`, and whatever holds the loop-bound lock — and each
names its hazard in its own docstring, so the next reader of a `mutants/` failure has the mechanism in
front of them rather than 45 generated names and no explanation. **The file that would show the
trigger had fired is `.github/workflows/mutants.yml`'s run history**, and the lesson of this ADR is
that naming it is not the same as anybody looking.
