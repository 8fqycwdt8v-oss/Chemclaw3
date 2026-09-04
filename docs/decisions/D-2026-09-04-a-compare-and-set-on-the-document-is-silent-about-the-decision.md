# D-2026-09-04-a-compare-and-set-on-the-document-is-silent-about-the-decision — a sign-off names the status it saw

**Status:** accepted · **Date:** 2026-09-04 · Extends
`D-2026-08-29-a-sign-off-names-a-revision-or-it-names-nothing`, which added `expected_revision`.
Closes the defect first recorded as open in
`D-2026-08-30-a-review-by-six-strangers-found-thirty-seven-defects`.

## Context

`protocols/store.py::set_status` took `expected_revision` — a compare-and-set on the *document*,
and never on the *status*. Two people looking at revision 1 could approve and abandon it and both
writes succeeded. Measured against Postgres, 100 `asyncio.gather` pairs of `approved` against
`abandoned` at one revision:

```
before     0 refusals — every pair took both writes
after    100 refusals — exactly one write per pair
```

Sequentially it needed no race at all: alice abandons at revision 1, bob approves at revision 1,
both succeed.

The evidence survived either way — `experiment_protocol_status_events` recorded both moves with
their actors and revisions, and the newest event agreed with the header 100/100 — so this was
"nobody is told at the time" rather than a lost record. What it cost is `advanced()`'s stated
guarantee that an `abandoned` design stays abandoned unless a *person* moves it: a second person's
`set_status` un-abandoned it silently, and a design retired because the starting material
decomposes was back in the `draft` listing.

## Decision

`expected_status` is a **required** field on `StatusIn`, on the `DesignStore` Protocol and on both
backends, checked against the status `_SELECT_HEAD` was already reading under `FOR UPDATE` and
discarding. No second read, no new lock. `require_unmoved` sits beside `require_movable` so one
rule serves both stores and a refusal cannot depend on which one answered.

**Revision is checked first.** When a revision lands on a decided design both compare-and-sets are
stale at once, and the document is the loss whose remedy is a diff a person reads.

### A new exception type, and deliberately not a subclass

`StatusConflict` is a sibling of `RevisionConflict`, not a code hung on it and not a subclass of
it. The route needs to *branch*, and it can only branch on the exception; reusing `RevisionConflict`
would mean inspecting a message. A **subclass** is worse than either: `post_status` catches
`RevisionConflict` first, so which `code` a caller receives would depend on the order of two
`except` clauses. The two share a status (409) and a remedy (re-read) but not a message, and that
difference is exactly what the UI needed.

### Required, not optional — and the UI gates rather than defaults

An optional `expected_status` that nobody sends makes the compare-and-set always agree: a control
that exists only in the docstring, which is the `map_to_hpc_identity` shape this tree deletes code
over, and which the backlog row warned about by name. So the field is required and
`Chemclaw3_ui` sends it.

`DesignOut.summary` is nullable, so the UI **gates the sign-off buttons on `summary !== null`**
rather than defaulting the field. Defaulting would have reintroduced the always-agrees case through
the front door.

## Consequences

**Two things the row did not mention, both fixed.** The status 409 was mis-kinded in the UI client
— `setProtocolStatus` had no 409 branch, so it fell through to `turn_in_flight` and rendered in a
neutral `role="status"` banner in the same tone as the success notice two lines above it. And
`moveStatus` was covered by **no test at all**: neither the component tests nor the e2e spec ever
clicked a Mark button, so "the UI actually sends it" would have rested on a client unit test alone.
A component test now clicks Mark and asserts the banner.

**`StatusConflict` had to be classified as non-retryable** (`durable/publish.py`). Temporal matches
non-retryable types by exact class name, so a new `ChemclawError` subclass is retryable until
somebody says otherwise — and a retried `abandoned` that silently overwrites somebody's `approved`
is the defect rather than the recovery. The drift guard that caught this is the one
`test_every_chemclaw_error_subclass_is_listed_non_retryable` exists to be.

**A guard enumerated a set the tree owns.**
`test_a_declared_but_unserved_tool_is_unverifiable_for_a_bundle_we_do_not_run` asserted
`{chem, safety}` because those were the declared-not-run bundles the day it was written, so wiring
a third turned a correct manifest into a red gate. It now derives the set from the property — an
endpoint declared and no `server/` package to ask. That is the **second** test in the same branch
found doing this; the first was the chart's all-disabled arm. Recorded together because the pair is
the finding: a test that enumerates a set the tree owns stops testing its property and starts
testing its own vintage.

**The `e2e/fixture-service.ts` guard is exercised by nothing today** — no spec drives the sign-off,
and Playwright browsers are not installed in the sandbox. It bites the moment such a spec is
written; the real coverage today is the component test.
