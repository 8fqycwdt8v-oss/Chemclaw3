# D-045 — F4-T2: workload identity federation (a pod mints its own token, no secret at rest)

**Context.** Backend components (front door, workers, MCP servers) must call Entra-protected
resources as themselves. Storing a client secret per component is the anti-pattern §7/ADR D-A4 rules
out. Entra Workload Identity Federation lets a pod present its projected ServiceAccount JWT as a
`client_assertion` in the OAuth2 client-credentials grant — no secret ever at rest.

**Decision.** `agents/identity/workload.py::WorkloadTokenProvider` performs that exchange and caches
per scope until `entra_token_refresh_leeway_seconds` before expiry; the SA token is re-read from
`entra_sa_token_path` on every exchange (it rotates). Transport and clock are constructor-injected so
the exchange is exercised offline against an `httpx.MockTransport` with a hand-cranked clock. A
process-wide `default_provider` + `get_service_token(scope)` convenience share one cache. Config:
`entra_workload_federation_enabled` (off in dev), `entra_workload_client_id`, `entra_token_endpoint`,
`entra_sa_token_path`, `entra_token_refresh_leeway_seconds`, `entra_http_timeout_seconds`.

**Consequence.** Any backend component can obtain its own Entra token with no stored secret; the LLM
generic credential remains the one documented exception (it does not use this path). Live tenant
exchange is the only gated edge — the code + request construction + caching are proven offline.
