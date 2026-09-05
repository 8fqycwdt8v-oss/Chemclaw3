# D-2026-09-04-a-contract-has-two-halves-and-a-server-test-sees-one — `expected_status`, and the sign-off nobody could make

**Status:** accepted · **Date:** 2026-09-04 · **The `expected_status` mechanism itself is
`D-2026-09-04-a-compare-and-set-on-the-document-is-silent-about-the-decision`**, which landed on
`main` from a concurrent branch while this one was open and is the canonical account of it — it has
the 100/100 concurrency measurement and the `advanced()` argument. Both branches reached "required,
not optional" independently. What is left to this ADR, and is not in that one, is *how the defect
was visible at all*: the client half.

## Context

`StatusIn` is `extra="forbid"`. `Chemclaw3_ui`'s sign-off panel has always sent a fourth field the
backend never declared. So **every protocol sign-off from the shipped UI was a 422** — not a
degraded path, the only path.

Verified against the client's `main` rather than inferred: `src/api/client.ts::setProtocolStatus`
posts `status`, `expected_revision`, `expected_status` and `reason`, and `StatusIn(**that_body)`
raises `extra_forbidden ('expected_status',)`. The client's `errorFromStatus` has always mapped a
`status_conflict` 409 to its own kind, against a backend that has never emitted one.

Every test in `tests/test_protocol_routes.py` passed throughout. Each one wrote the body the
**server** expected, which is the one shape a server-side test cannot check on its own. Twenty-seven
route tests, `mypy --strict`, ten validators and a 6,406-test suite were all green over an endpoint
its only client could not call.

It was found by a fixer working an unrelated finding, who read a claim in `set_status`'s own
docstring — that the change was blocked on "a contract change across `Chemclaw3_ui` as well" — and
checked it against the client instead of believing it. The claim was stale; the client half had
shipped.

## Decision

The mechanism is the other ADR's: `expected_status` is required rather than optional, and
`require_unmoved` runs before `require_movable`, because "somebody moved this while you were
reading" is the more actionable answer than "that move is illegal". This branch's own contribution
to the code is the lifecycle table `require_movable` now consults, and the test below.

**What this ADR decides is a review practice.** Where a route's only caller is a companion
repository, the test that matters is written from **that repository's source**, not from the
server's model. `tests/test_protocol_routes.py` now contains a test that sends the client's literal
body, field for field, rather than a parametrised case — so a field the panel adds later fails there
rather than in somebody's browser.

## Consequences

The general rule, which is the part worth citing later: **a green server suite is not evidence that
a client can call the server.** This tree has four companion repositories and every seam to them is
a contract with a half nobody here runs. Where a route's only caller is a companion repo, the test
that matters is the one written from that repo's source.

What this does *not* establish is a mechanism. Checking a client's source by hand is what found this
one, and it does not scale to four repositories; a generated or shared contract fixture is a
`BACKLOG.md` row, not a decision taken here.
