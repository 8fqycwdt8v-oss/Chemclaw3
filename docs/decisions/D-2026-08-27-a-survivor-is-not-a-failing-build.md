# D-2026-08-27-a-survivor-is-not-a-failing-build — the weekly mutation run gates on the kill rate, not on the survivors

**Status:** accepted

## Context

`[tool.mutmut]` has named seven invariant-bearing modules since it was written — the authorization
gate (`agent/authz.py`), the audit sink (`agent/audit_store.py`), the budget guard
(`api/budget.py`), the runner's trace (`api/runner_trace.py`), the note schema (`kg/note.py`), the
PR-gate (`kg/pr_gate.py`) and the calculation cache's key (`science/calc/store.py`). `make mutants`
is deliberately outside the `ci` target because it costs an order of magnitude more than the gate.

It also had **no schedule**, which is the part that made it a claim rather than a control. The only
thing that had ever run it was a person, once
(`D-2026-08-08-a-survivor-is-a-hypothesis`) — and that pass found real holes: `propose_note`'s
`actor`, `session_id` and `correlation_id` stamps could each be deleted with the whole suite green
(which also makes a proposal its own author is served a 404 for), and the reviewer-facing
`with N supporting note(s)` count in the PR body was asserted by nothing. A control with that track
record, run once, is a control whose next regression nobody hears about.

Adding a weekly job is easy. Deciding what it does with a result is the decision, because the
obvious answer is wrong in both directions: fail on any survivor and the job is red forever; fail on
nothing and a weekly green job is indistinguishable from a weekly job that stopped running.

## What was measured

Run twice on this checkout, 2026-08-27, four cores, against real Postgres — the same commit, the
same configuration, no edit between them:

```
run 1 (warm cache)   killed 618 · survived 171 · no_tests 34 · timeout 2 · suspicious 0 · segfault 0 · total 825
run 2 (cold, mutants/ deleted)  killed 634 · survived 155 · no_tests 34 · timeout 2 · suspicious 0 · segfault 0 · total 825
```

**Sixteen mutants changed verdict between two runs of identical code** — 74.9% and 76.8% kill rate,
a 1.9-point spread — while `no_tests` and `timeout` were identical both times. That is the most
useful number here and it was not the one this ADR set out to collect: the *survivor set itself is
not reproducible to better than about two points*, which is an independent reason not to gate on it
and a caution against reading a week-on-week survivor delta as a change in the code.

By module, from run 1, the two categories that are not `killed`:

| module | survived | no tests |
| --- | --- | --- |
| `science/calc/store.py` | 64 | 3 |
| `api/runner_trace.py` | 58 | 0 |
| `kg/pr_gate.py` | 21 | 0 |
| `kg/note.py` | 20 | 10 |
| `agent/authz.py` | 7 | 15 |
| `agent/audit_store.py` | 1 | 6 |
| `api/budget.py` | 0 | 0 |

Two facts decide the policy.

**A zero-survivor gate would be red on the day it merged**, and for reasons already adjudicated:
seventeen of the survivors in `note.py`, `pr_gate.py` and `audit_store.py` were examined one by one
in `D-2026-08-08-a-survivor-is-a-hypothesis` and are equivalent or harmless. The two modules that
dominate the count — `runner_trace.py` and `science/calc/store.py` — have never had such a pass at
all, so most of their 122 are unclassified rather than known-bad. Neither is a defect this workflow
found; both are the standing state of a repository that runs mutation testing occasionally.

**`no_tests` and `timeout` are not zero either**, which is what a first draft of the workflow
assumed from the ADR above. That ADR's zeros were measured over *three* modules after its selection
was corrected; over all seven, 34 mutants are reached by no selected test and 2 time out
(`PostgresAuditSink.flush`, `cached_compute` — both Postgres paths, and therefore the two scores
here with real run-to-run variance). A gate asserting a number this repository has never actually
held is a gate that fails on its first fire and gets disabled.

**The run is cheap enough to schedule.** 825 mutants in **2m57s** warm and **3m45s** cold (the
difference is one stats collection, which a CI runner always pays). mutmut parallelises over `os.cpu_count()` and runs only the tests its stats
say touch each mutant, so the cost tracks the seven modules rather than the suite.

## Decision

**A weekly `.github/workflows/mutants.yml`, gating on the kill rate against a recorded floor, and
publishing everything else.**

1. **Weekly, Monday 02:00 UTC** — the cadence `dependabot.yml` and `ci.yml`'s `deps-audit` schedule
   already use, an hour apart so the two long jobs do not start together. `workflow_dispatch` for a
   branch that touches one of the seven modules.

2. **Real Postgres, and this is not symmetry with `ci.yml`.** Six of the eighteen files in
   `pytest_add_cli_args_test_selection` gate on `tests/pg.py::migrated_db_or_skip`. Without a
   database they skip and still report green, so every mutant in `science/calc/store.py` and
   `agent/audit_store.py` would be scored SURVIVED for a reason that has nothing to do with the
   mutation — manufacturing survivors in exactly two of the modules the job exists for.

3. **The gate is `killed / total` against a floor recorded in the workflow**, with the measurements
   and their date beside it. Observed 74.9% and 76.8%; the floor is **72.0%** — roughly one and a
   half times the observed 1.9-point spread below the *lower* of the two runs. Sized against the
   measured variance rather than rounded down from the best result, because a floor set just under
   one observation of a quantity that moves two points is a job that fails for no reason and gets
   switched off.

   Two runs is a small sample and the floor is provisional on exactly that. The 90-day artifact is
   what makes it revisable: after a few Mondays there is a distribution to set it from, and raising
   it is then a deliberate edit with a fresh measurement beside it.

   A rate rather than a count, because a count breaks the first time a module legitimately grows —
   adding well-tested code raises `killed` and `total` together and leaves the rate alone, which is
   the property that lets the floor survive ordinary work.

4. **A survivor never fails the job.** `D-2026-08-08-a-survivor-is-a-hypothesis` established by
   measurement that a survivor from this selection is a claim about the *selection*: 29 of that
   run's 39 survivors were killed by two test files the selection did not name. The two runs above
   add a second leg to the same conclusion — a survivor is not even a *stable* observation. Acting
   on one without re-running it against the whole suite produces tests for behaviour already pinned.
   So survivors are published — the counts in the step summary, the full non-killed list as a 90-day
   artifact — and the week-on-week delta is readable without a gate that cries every Monday.

5. **`suspicious` and `segfault` do fail**, at zero, because they are not results at all: the
   harness went wrong, and every number above them is unexplained until that is.

6. **A failure files an issue.** GitHub's own notification for a scheduled workflow is an email to
   whoever last edited the cron line, plus a red row in a tab nobody opens on a Monday. That is the
   same defect as having no schedule, one step further along, so the job opens (or comments on) an
   issue labelled `mutation-testing` — deduplicated on the label, so three bad weeks are one thread
   rather than three issues nobody closes.

## Consequences

- The ~160 survivors and 34 no-tests are **recorded, not fixed**. `runner_trace.py` and
  `science/calc/store.py` between them hold 122 survivors and have never had the module-by-module
  pass the other three got; that is a piece of work, not a line in this change, and it is the
  obvious use of the artifact this job now produces every week.
- The floor moves **up** when somebody does that work, and moving it is a deliberate edit with a
  fresh measurement beside it — the same discipline `EVAL_CASE_SET_VERSION` and
  `data/evals/baseline.json` already carry for the science gate.
- `make mutant-stats` is new: `mutmut export-cicd-stats`, so the workflow decides on a number rather
  than by grepping the human-facing `mutmut results` output.
- Nothing about the seven-module scope changes here. Whether that list is still the right one is a
  separate question this run does not answer.
