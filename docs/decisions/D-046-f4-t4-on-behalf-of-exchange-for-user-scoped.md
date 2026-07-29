# D-046 — F4-T4: On-Behalf-Of exchange for user-scoped downstream (wired, dormant)

**Context.** When a backend acts for a *specific user* against a user-scoped resource (ELN/LIMS), it
must present the user's identity downstream, not its own service identity. OAuth2 OBO (RFC 7523)
exchanges the user's token for a downstream-scoped one.

**Decision.** `agents/identity/obo.py::exchange_obo(user_token, scope)` performs the OBO grant,
authenticating to the token endpoint with the federated SA assertion (`read_sa_token`, shared with
F4-T2 — one reader, two callers, no duplication). Transport injected for offline tests. Config
`entra_obo_enabled` (off). It is deliberately **generic and dormant**: no user-scoped source exists
yet (the first, a custom Snowflake ELN connector, is deferred behind the F7 seam), so nothing calls
it — a source opts in later. This is the wired-but-unused seam the ticket asks for, not a dead stub:
it is the single mechanism every user-scoped source will use.

**Consequence.** OBO is available for any future user-scoped source; the exchange, the OBO assertion,
and the federated client-assertion are proven offline; the live tenant exchange is the only gated edge.
