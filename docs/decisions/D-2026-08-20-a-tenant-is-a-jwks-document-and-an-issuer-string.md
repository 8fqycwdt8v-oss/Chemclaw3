# D-2026-08-20-a-tenant-is-a-jwks-document-and-an-issuer-string — the enforced identity path is proven, not asserted

**Status:** accepted · **Date:** 2026-08-20 · Closes the `DEFERRED.md` claim that proving F4 needs a
real Entra tenant. Does not supersede D-043 (the design), D-060 (per-tool authorization) or
D-067 (fail-closed startup); it is the measurement those three never had.

## Context

An authentication audit read every identity surface across `Chemclaw3`, `Chemclaw3_ui` and
`Chemclaw3_mock`. It found no exploitable bypass — every gate it probed held — and one structural
gap that explains the rest: **the enforced path had never run.**

- `infra/live/processes.sh:45` pinned `CHEMCLAW_ENTRA_REQUIRED=false`.
- `infra/live/e2e-full-stack/up.sh:236` started the UI with `AUTH_MODE=dev`.
- `Chemclaw3_mock` mocked an HPC launcher, two ELN sources and an MCP tool — and no identity
  provider.
- `tests/test_auth.py` proved the *validator* thoroughly (21 cases) and proved nothing about the
  chain, because its fixture swaps `auth._signing_key` for a lambda: the JWKS lookup, the one part
  of validation that touches a network, never ran in any test.

So every deployment ran a posture that no lane exercised, and `docs/planning/DEFERRED.md` recorded
this as gated on infrastructure — "real token validation" awaiting "a real Entra tenant".

**That was never what it needed.** A tenant, *to a resource server*, is a JWKS document and an
issuer string. Nothing about proving the chain requires Microsoft.

## Decision

**Stand up a tenant and run the enforced posture, in CI and in the live lane.**

`Chemclaw3_mock` gains `app/entra/`: one published signing key, an RS256 mint that issues whatever
`oid`, roles and groups a caller asks for, and a discovery document. Off by default
(`MOCK_ENTRA_ENABLED`) — a mint with no client authentication is a credential forge wherever it is
reachable, and that default is the control rather than a convenience.

Half of authentication is what gets *refused*, so every way to be invalid is one field on the same
request rather than a separate endpoint: `audience`, `issuer`, `expires_in` (negative for expired),
`omit_expiry`, and `unpublished_key` — signed by a second key the JWKS deliberately does not
publish. A mock that can only mint valid tokens cannot ask whether forgeries are rejected.

`tests/test_entra_end_to_end.py` is the CI half: a real HTTP server on a real port serves the JWKS,
`settings.entra_jwks_url` points at it, PyJWT fetches it with its own urllib, and `create_app()` is
the production app with `entra_required=True`. Nothing inside the module under test is patched.

`infra/live/processes.sh` gains the enforced posture behind `CHEMCLAW_LIVE_ENTRA_TOKEN_URL`, mints
the probe identity from the same issuer the front door validates against, and persists the minted
credentials so a second terminal presents what the running processes hold.

## What the proof actually shows

Run against the live stack (front door, four Temporal workers, four connector servers, Postgres,
Temporal), `CHEMCLAW_ENTRA_REQUIRED=true`:

| request | result |
|---|---|
| no token · garbage token | 401 |
| signed by an unpublished key | 401 |
| wrong audience · wrong issuer | 401 |
| expired · no `exp` claim at all | 401 |
| a token this tenant vouches for | 200, session opened |
| `DELETE /jobs/x` — token with no roles | 403 |
| `DELETE /jobs/x` — token with `process-chemist` | 404 (past the gate; no such job) |
| alice reads her own session | 200 |
| bench reads alice's session | 404 `unknown session` |
| `/healthz` `/readyz` `/metrics` | 200 |
| `/` (the bundled UI) | 404 |

A full turn then ran through the enforced front door, and `audit_events.actor` recorded `u-alice` —
the `oid` from the minted token, validated at the edge, carried through the ambient identity into
the audit sink. That row is the chain.

**The fourteen CI tests are mutation-proved**, because a test that has never been seen to fail is
not known to catch anything. Seven deliberate regressions, seven caught: audience verification
disabled, the `exp` requirement dropped, the JWKS refresh cooldown removed, an IdP outage raised as
a bad credential, the `oid` check replaced by an anonymous default, the ownership check disabled,
the operator role gate removed. Two earlier attempts were *invalid* mutations and are recorded as
such in the test module — removing the `audience=` argument makes PyJWT reject every token carrying
an `aud`, and subclassing `IdentityProviderUnavailable` from `AuthError` changes nothing because its
`except` clause is ordered first.

## What is still not proven, and why

**The browser leg.** MSAL talks to `login.microsoftonline.com`; mocking that is mocking Microsoft's
login UI, not a JWKS, and a UI auth mode that accepts tokens from an arbitrary issuer would be the
`ALLOW_DEV_AUTH` hazard rebuilt. What the frontend contributes is instead pinned by tests in that
repo: sixteen cases over the MSAL provider (which had none), and a BFF proxy test asserting the
bearer reaches the upstream byte for byte with cookies dropped. The remaining gap is one hop —
browser to tenant — and it is named here rather than papered over.

## Consequences

- `DEFERRED.md`'s "real token validation" row is deleted: what it waited for is done, and what
  remains (the browser leg) is stated above rather than tracked as infrastructure.
- A lane can be run enforced by anyone, offline, in about a minute.
- The mock's tenant is a credential forge by design. `MOCK_ENTRA_ENABLED` defaults off and the
  README says why in the imperative.
