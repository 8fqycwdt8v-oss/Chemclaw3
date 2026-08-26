# D-2026-08-26-an-entitlement-set-is-not-provenance — `X-Chemclaw-Roles` is removed from the connector wire

## Status

Accepted. Found in the same three-seam review as
`D-2026-08-26-a-request-timeout-bounds-the-wait-not-the-work`.

## Context

`connectors/identity.turn_headers` stamped five headers on every connector request. Four say who is
asking and which turn this is; the fifth, `X-Chemclaw-Roles`, carried the caller's entire
entitlement set, space-delimited and sorted.

It had one writer and no readers. Measured across both repositories: nothing in `src/` here reads
it — `connectors/server.CallerLogMiddleware` logs actor, session and dry-run — and nothing in
`Chemclaw3-mcp` reads it either, where `mcp_server_kit.identity` declares four header constants and
`X-Chemclaw-Roles` is not among them. The only occurrences outside its own definition were three
assertions in this repository's own tests, checking that the thing was sent.

**That it had no reader is not an oversight to correct; it follows from the rule above it.** The
module's own docstring says the headers "are advisory, never authorization" and that a connector
"must never make an access decision on a header's word". `agent/tool_authz` and `agent/audit` run
here, before the call leaves the process. So the one use an entitlement set has is the one use a
connector is forbidden. What is left is correlation, and correlating a connector's records with the
audit trail needs the actor and the correlation id — which is exactly what the other headers are.

Meanwhile it was the one identity header with no bound on its size. Under
`entra_group_claims_as_roles` the role set includes every AD group in the token, prefixed; a user in
many groups produces a header of several kilobytes, sent on every tool call, to every connector —
including servers this family does not host, where the manifest supplies only an address
(`D-2026-08-09-a-connector-we-do-not-run`). The users it grows longest for are precisely the ones
`api/auth._principal_from_claims` already logs a warning about.

So the header exported a user's full group membership across a trust boundary, on every call, for
nobody.

## Decision

`X-Chemclaw-Roles` is removed: from `turn_headers`, from `STAMPED_HEADERS`, and from the module's
constants. `get_current_roles` is untouched and keeps every one of its real callers — `agent/authz`,
`agent/skill_access`, `agent/durable_tools`, `templates/registry`, `ingest/documents/retriever` —
all of which are in-process gates that decide before a call is made.

`tests/test_connector_identity.py::test_the_callers_entitlements_are_not_sent_to_a_connector` is an
*absence* test, the shape `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`
established for a claim with no producer, applied to the mirror case: a value with no consumer. It
asserts no header on a connector request names a role.

## Consequences

- What travels to a connector is now the minimum that makes the audit trail joinable: actor,
  session, correlation id, dry-run, and W3C trace context.
- Re-adding it is a decision rather than a line. It needs a connector that actually reads it and an
  argument for why an entitlement set is what that reader needs, given that no connector may decide
  on one — and a bound, since the token's group claim has none.
- `STAMPED_HEADERS` is one entry shorter, which narrows the redirect-strip in `turn_identity_hook`
  to exactly what is still sent. That guard is unchanged in kind: the invariant is "everything of
  ours is removed on a foreign origin", and the tuple is still the single list both it and
  `tests/test_connector_identity.py` read.
