# D-2026-09-04-fifteen-fresh-contexts-over-one-tree — what a deployment-readiness review found, and what the method itself measured

**Status:** accepted · **Date:** 2026-09-04 · Method ADR for the hardening pass whose individual
decisions are `D-2026-09-04-a-name-is-one-capability-across-every-namespace`,
`D-2026-09-04-a-job-that-suspends-on-a-person-carries-no-ceiling`,
`D-2026-09-04-a-contract-has-two-halves-and-a-server-test-sees-one` and
`D-2026-09-04-the-configurations-this-tree-now-refuses-to-start-in`.

## Context

The question asked was whether this tree is deployable and maintainable, not whether any one module
is correct. Fifteen reviews were run against a **running** stack — `dockerd`, `make up`,
`make db-migrate` — so the Postgres-backed half of the suite was evidence rather than a skip, and
each reviewer was told the same thing this repository tells itself: a finding carries the command
and its output, or it is a hypothesis and ranks below everything that was run.

Six sweeps cut across the tree (deployment surface, security, concurrency and resources, failure
modes under fault injection, observability, dead code and prose drift), eight read one package each,
and one asked whether the suite can fail at all.

The baseline: **6,406 passed, 1 failed, 17 skipped**. The single failure was not an assertion.

## What it found

78 findings, 26 of them HIGH. The distribution is the interesting part, because it is not where a
reading of this repository's own documents would have predicted.

**The deployment surface was the worst-served part of the tree**, and it is the part every other
part depends on. `helm rollback` restored the pods and left the previous release's configuration in
place, because `chemclaw-config` and the runtime ServiceAccount were hook-scoped and so absent from
what Helm tracks. Two feature flags rendered manifests the Kubernetes API server rejects. The
Jenkins release path could not render the chart at all, and where it did deploy it used
`helm upgrade --atomic`, which this chart's own ADR forbids. The build context was 6.9 GB carrying
`.env`, `*.pem` and `.git`, because `deploy/.dockerignore` sat where Docker never reads it.

Every one of those had been reviewed before. None had been *rendered* before: `helm` is not
installed in this sandbox, twelve chart assertions skip silently as "helm is not installed", and the
skip epilogue this repository built to make skips loud does not cover that one. Installing `helm`
was the single highest-yield act of the pass.

**Three defects were the same shape in three packages**: a control that exists, is asserted by a
test, and is not reached by the thing it protects. `find_past_jobs` framed two columns of
`job_records` while `_recorded_status` read the same two columns twelve lines above it and returned
them raw. `_declared_tool_names` refused a connector name colliding with another connector's and
never with a first-party tool's. The `parent_close_policy` scan test named `connectors/bo/workflows.py`
in its prose as a caller it was not reading, which is why that bundle's fourteen-day wait — the
longest-lived row in the system — kept its stranding bug through the commit that fixed the identical
bug next door.

**Numbers in prose went stale inside the commit that wrote them.** `#305` raised
`agent_tool_result_clear_trigger` from 30,000 to 73,500 and, in the same diff, left three
present-tense sentences saying it "is 30,000" and one calling the raise "an open decision this
repository has not taken". Seven such claims were corrected, including a class name —
`PostgresAuditStore` — that three maintained documents cite as the evidence the audit trail is
write-only and that **has never been defined in this repository's history**.

## Decision

The findings are fixed in the commits this ADR indexes, and four things are recorded as method.

**1. A gate that cannot run is not a gate, and its skip must be as loud as a failure.** `helm` is
the case in point. `tests/pg.py`'s epilogue exists because a Postgres skip once cost this repository
coverage silently; the chart skips had no such epilogue and cost it five HIGH defects.

**2. Where two readers share one store, assert the property of the *store*, not of either reader.**
The framing fix is written as a property of the record path rather than of `get_durable_job_status`,
because a per-tool test would not have caught two readers of one table disagreeing.

**3. A number in prose is a claim about a commit, so derive it or do not write it.** Applied again
here: the middleware count is now `len(tool_call_middleware(...))`, the operations tables are named
rather than counted, and `test_docstring_paths`' hand-written list of Helm helper names is derived
from `_helpers.tpl` — that list going stale is what failed four files for prose that was correct.

**4. A server-side test cannot check a contract's other half.** This is
`D-2026-09-04-a-contract-has-two-halves-and-a-server-test-sees-one`, and it is the finding of the
pass that no amount of in-repo review would have produced.

## Consequences

The suite is stronger than the tree it tests: 22 positive-control mutations across authz, netguard,
auth, identity, redaction, outbox, retention, framing and subagents were all killed, there is no
`continue-on-error` or `|| true` in CI, and no drift between `make ci` and the workflow. Three
mutants survived and are recorded in `docs/planning/BACKLOG.md`; the largest is `core/fulltext.py`'s
tokeniser, which can revert to the exact bug its own comment names while 349 retrieval tests across
22 files stay green.

The one baseline failure was `test_bo_campaign_finds_high_yield`, which measured **279 s** against a
180 s cap whose own comment justified itself by naming that test as "~37s" — stale by 7.5x, in the
file that configures the gate. It carries a marker now, because it is slow rather than hung and that
was measured rather than assumed.

**Not settled by this pass, and named so nobody reads its absence as an answer**: the review ran
against a stack with no OpenShift cluster and no real Temporal broker beyond the dev server, so the
live edges `docs/planning/BACKLOG.md` records are still open. A rendered chart is not a deployed
one.
