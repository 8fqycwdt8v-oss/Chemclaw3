# D-2026-08-20-a-ui-that-cannot-authenticate-is-not-a-fallback — three smaller findings from the identity audit

**Status:** accepted · **Date:** 2026-08-20 · The remainder of the audit that
`D-2026-08-20-a-tenant-is-a-jwks-document-and-an-issuer-string` opens. Three unrelated findings, one
ADR, because each is a paragraph and none is a decision anyone would look for by name.

## 1 — The front door served a chat UI that cannot authenticate

`src/chemclaw/api/static/` is a working chat client and contains **zero** occurrences of the string
`Authorization` — measured, in both the HTML and the JavaScript. It subscribes to job push-back with
a native `EventSource`, which cannot carry a header at all. The mount was unconditional,
`deploy/Containerfile` copies all of `src/`, and the OpenShift Route declares no `spec.path`, so it
sat on the public host beside the API.

Under the shipped chart — `CHEMCLAW_ENTRA_REQUIRED=true` — every call it makes is a 401 on the first
click. It fails closed, so this was never a bypass. It was a permanently broken application shell on
a public host, competing for `/` with `Chemclaw3_ui`, which does authenticate.

**Mounted only when identity is not enforced.** A setting was considered and rejected: there is no
configuration under which this UI works with identity on, so a knob would only offer a way to be
wrong. What the condition buys is worth more than the tidiness: `tests/test_route_auth_coverage.py`
could previously say only "there is exactly one ungatable route and we know what it is", because a
`Mount` carries no dependency tree. It now asserts that an *enforced* app has no ungatable surface at
all — every route FastAPI serves resolves through `require_principal`.

## 2 — Three Entra settings had no reader, and one shipped in the ConfigMap

`entra_token_endpoint`, `entra_sa_token_path` and `entra_token_refresh_leeway_seconds` were read by
nothing in `src/`. Their code — workload identity federation (F4-T2) and the On-Behalf-Of exchange
(F4-T4) — was deleted unused by `D-2026-08-15-a-capability-that-ships-off-is-not-a-capability`, and
the settings survived on the argument that they describe the tenant rather than the mechanism.

That argument does not survive contact with `values.yaml`, which shipped
`CHEMCLAW_ENTRA_TOKEN_ENDPOINT` as a rendered ConfigMap value. A reader of the chart saw a configured
OAuth token endpoint and concluded something exchanged tokens there. Nothing did. That is the
`map_to_hpc_identity` shape one layer up — a *deployment* declaring a credential path that does not
exist — so the three go the way their code went. D-046 stands as the design for whatever re-adds one.

`entra_http_timeout_seconds` stays: `api/auth._client_for` builds the `PyJWKClient` with it, so a
slow or blackholed IdP is bounded by our config rather than by PyJWT's 30-second default.

## 3 — A group-claim overage was a log line, and nothing watches log lines

Past roughly 150 group memberships Entra replaces `groups` with `_claim_names`. There is no fix at
request time — resolving the overage needs a Graph call, which D-089 does not permit — and
`api/auth.py` handled it honestly: it logged a WARNING and proceeded with no group-derived
entitlements rather than pretending the user had none.

The gap is that this is silent from *both* sides. The chemist sees a group-gated document share
return nothing, which is indistinguishable from a share with nothing in it; the operator sees a
warning on a pod's stdout, which nobody watches. So the users with the **most** access lose every
group-derived entitlement and no signal moves.

`chemclaw_group_claim_overage_total` is now declared and incremented beside the log line, with a
`ChemclawGroupClaimOverage` rule in the chart's `PrometheusRule`. Warning rather than critical:
nothing is broken, someone is quietly under-entitled, and the remedy — assign the group to an app
role, so it arrives in the `roles` claim — is in the alert's description. The counter is unlabelled,
because a label carrying the `oid` would key an unauthenticated exposition on user identity.

## Also carried out here

`chemclaw.cli.live_jobs` binds an identity before launching its job. It drives a *user-triggered*
durable job, so `prepare_job_launch` calls `require_actor()`, which under `entra_required` refuses
work with no authenticated user — the F4-T3 rule, and correct: a `job_records` row whose
`requested_by` is nobody answers no question. Unbound, the tool simply could not run in the posture
the chart ships, which is the one worth smoking. It resolves through `cli.chat.resolve_identity`
rather than reading the same two settings again, so it inherits "--admin bypasses *authentication*
only" and cannot invent an entitlement this deployment never granted.
