# Deployment-readiness review and hardening — 2026-09-04

Branch `claude/code-review-hardening-lchtz5`. Goal: prove the tree is deployable, robust and
maintainable, and fix what proves it is not. Every finding is a claim about a commit, so every
finding carries a command and its output or it does not ship.

## Method

Fifteen fresh-context reviews, each with its own scope and no shared belief about the tree, over a
running stack (`dockerd` + `make up` + `make db-migrate`) so the Postgres-backed half of the suite
was evidence rather than a skip. Six cross-cutting sweeps, eight per-package reads, one asking
whether the suite can fail. Then nine fixers on disjoint file territories, each required to
reproduce a finding at `HEAD` before fixing it and to write the failing test first.

## Steps

- [x] Baseline `make lint type test`: **6,406 passed, 1 failed, 17 skipped**.
- [x] Wave 1 — fifteen reviews. **78 findings, 26 HIGH.**
- [x] Wave 2 — triage; every fix reproduced at `HEAD` first. Several findings did not survive that
      and were dropped; two reviewer-proposed fixes were measured and **rejected in favour of
      something else** (see Review).
- [x] Wave 3 — fixes, each with a test that fails before and passes after.
- [x] Wave 4 — five ADRs, two `BACKLOG.md` rows closed, seven opened, full gate, PR.

## Review

**Result: `make lint type test` green — 6,551 passed, 4 skipped, 0 failed.** From a baseline of
6,406 passed / 1 failed / 17 skipped: +145 tests, the failure fixed, and thirteen fewer skips
because `helm` was installed.

**What the method got right.** Installing `helm` was the single highest-yield act of the session.
Twelve chart assertions skip in this sandbox as "helm is not installed", the epilogue this
repository built to make skips loud does not cover that one, and five HIGH chart defects had
survived every previous review because nobody had ever rendered the chart. A gate that cannot run
is not a gate.

Requiring reproduction-before-fix paid for itself repeatedly. Fixers rejected reviewer prescriptions
on measurement four times: `ABANDON` as a parent-close policy is *worse* than the default, not
better; writing an ingest record before its index rows breaks the invariant the replay-skip rests
on; a 503-on-`None` for the plan routes would 503 every new conversation, because a healthy
checkpointer and an outage are byte-identical there; and `Component.attributes` cannot hold
per-product yields because nothing renders them for outcomes.

**What the method missed, and what caught it.** Each fixer ran `mypy --strict` over its own `src/`
package and reported clean; the gate runs `mypy src examples tests`, and eleven errors were in the
tests. Then the full suite found three regressions this branch itself introduced — a new error class
not registered non-retryable, a new `DELETE` with no grant, and `core/config` acquiring a
module-scope `psycopg` import that the datasource-isolation seam forbids transitively. All three
were caught by gates this repository had already built. A per-package check is not the gate.

**The finding no in-repo review could have produced**: `StatusIn` is `extra="forbid"` and the
shipped `Chemclaw3_ui` has always sent `expected_status`, so **every protocol sign-off from the UI
was a 422** — while twenty-seven route tests, `mypy --strict`, ten validators and a 6,406-test suite
stayed green, because each test wrote the body the *server* expected.

**Open, and named so its absence is not read as an answer**: no OpenShift cluster and no real
Temporal broker beyond the dev server, so the live edges in `docs/planning/BACKLOG.md` stand. A
rendered chart is not a deployed one. Seven new rows are queued, three of them mutants that survive
— `core/fulltext.py`'s tokeniser can revert to the exact bug its own comment names while 349
retrieval tests stay green and it measures 100% line and branch coverage doing it.
