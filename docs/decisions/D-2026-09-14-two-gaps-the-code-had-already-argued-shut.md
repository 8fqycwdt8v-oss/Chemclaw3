# D-2026-09-14-two-gaps-the-code-had-already-argued-shut — the UI's refusal of `/schedules` and the fleet's ownership of `resolve_compound` are arguments, not omissions, so both planned items are retracted

## Status

Accepted. Retracts two Wave 1 items of
`docs/archive/REVIEW-2026-09-13-capability-audit-and-plan.md` ("close the two UI/API gaps").

## Context

The 2026-09-13 capability audit's Wave 1 line listed, among six items, *"close the two UI/API
gaps"* — meaning: the UI does not consume `GET /schedules`, and `resolve_compound` has no HTTP
route. Both were written from a reading of what is served against what is called, and neither was
checked against the argument for the difference. Both turn out to be deliberate, and one of them is
a rule this repository enforces elsewhere.

## What the measurement found

**`GET /schedules` is not unconsumed; it is refused, in three places.** `Chemclaw3_ui`'s BFF
whitelist (`server/routes.ts`) names it explicitly among the routes *"this UI has no business
reaching"* beside `/metrics` and `/events/knowledge-merged`, `tests/routes.test.ts` asserts that
`resolveRoute('GET', '/api/schedules')` is `null`, and `scripts/check-openapi.mjs` excuses it by name
from the contract-drift check so the omission does not read as drift. The backend's own framing
agrees: `api/routes/ops.py` is an operator surface, and `api/deps.py` names `/schedules` as one of
the *"authenticated, deliberately unscoped"* routes — a caller must exist for the rate budget, but
the answer does not depend on who is asking. Building a chemist-facing page for it would flip a
whitelist assertion and widen the BFF's blast radius, which is a decision about the proxy and not a
gap in a capability.

**`resolve_compound` is not a function in this repository.** It is a tool name served by
`Chemclaw3-mcp`'s `servers/chem/`, present here only as a line in
`connectors/chem/connector.yaml`, whose own header says *"This bundle is a declaration, not a
server — we do not run it."* Adding an HTTP route in core that answered the same question would be a
second implementation of a capability the fleet serves, which is precisely what `CLAUDE.md`'s "Never
duplicate a Chemclaw3 capability" and `connectors/README.md`'s two-live-definitions-of-`predict_pka`
record forbid. The in-process near-namesake, `core/reagents.py::resolve_compound_name`, is a
different function with a different contract — a curated synonym table with no tool surface, used by
`memory/progression.py`, the ORD adapter and `protocols/checks.py` — and exposing *it* over HTTP
would publish a second answer to the same question rather than close a gap.

## Decision

**Both items are retracted and neither is built.** The plan artifact is a dated record and is not
rewritten; this is the correction, and `docs/planning/BACKLOG.md` carries no row for either, because
there is nothing pending.

Reopening either is a new decision with a named subject: for `/schedules`, whether the BFF whitelist
should carry an operator surface at all (and if so, gated on what); for compound resolution, whether
any HTTP route in this repository may wrap a fleet tool, which is a seam-wide question rather than a
question about one tool.

## Consequences

- Wave 1 ships five items rather than six, and the axis those two were charged against
  ("Producing results", 60%) is unmoved by their absence: neither was on the delivery path that
  `D-2026-09-14-a-declared-kind-with-no-producer-is-not-a-channel` opens.
- **The audit's own method is the thing corrected here.** Both findings were "X is served and Y does
  not call it", which is a true observation and not a defect; the missing step was reading the
  argument for the difference before scheduling the work. That is the same failure mode this
  repository's prose rules are about, one level up — a claim about a commit, asserted rather than
  measured.

## What keeps it true

Nothing new, deliberately: the controls that make these arguments hold are already asserted, and
adding a test to pin a decision not to build something would be the `map_to_hpc_identity` shape —
a claim that a control exists.

- `Chemclaw3_ui/tests/routes.test.ts` — `/api/schedules` resolves to no route, in both methods.
- `Chemclaw3/tests/test_route_auth_coverage.py` — `/schedules` is outside the probe allowlist and
  requires a principal.
- `Chemclaw3/tests/test_validate_connectors.py` — `chem` is a declaration this repository does not
  serve, so a second local implementation of one of its tools would have to displace the manifest
  rather than sit beside it.
