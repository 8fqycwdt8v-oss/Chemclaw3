# D-043 — F4: Entra ID identity & RBAC — front-door OIDC + one authorization gate (D-A4)

**Context.** Identity via Entra is a hard requirement, and it becomes load-bearing the moment the
harness can autonomously trigger expensive HPC/BO paths ("who asked", "may they"). F4 makes
`architektur.md` §7/§8 real; the offline-verifiable core landed first, the tenant/federation edges are
infra-gated.

- **F4-T1 front-door OIDC.** `service/auth.py` validates every non-health request's Entra JWT —
  RS256 against the tenant JWKS, **audience** checked (confused-deputy: the front door is client *and*
  resource), issuer checked — into a `Principal(oid, upn, roles)`; `require_principal` is the FastAPI
  guard (401 without a valid token). `entra_required` gates enforcement; off in local dev (a stand-in
  principal), on everywhere real. JWKS/issuer derive from `entra_tenant_id`. Dep `pyjwt[crypto]`;
  bugbear allows the `fastapi.Depends` idiom. Tested with locally-signed RSA tokens (no network).
- **F4-T5 one authorization point + real actor.** `agents/authz.py::authorize_trigger(action)` is the
  single gate: an action in `entra_expensive_actions` needs a user holding an `entra_privileged_roles`
  role, else `AuthorizationError` — checked before the durable job starts, so an autonomously-planned
  todo can't launch an expensive path outside the user's entitlements (open in dev). The turn's
  identity is **ambient** (`agents/identity_context.py` contextvar, stamped by the runner from the
  `Principal`, like the session id), so the audit middleware records the real Entra oid over its
  build-time default, and `submit_qm_job` both authorizes and stamps `requested_by` = oid — all
  without rebuilding the per-process agent. `requested_by` stays out of `qm_job_key` (D-011).
- **Deferred / infra-gated:** workload identity federation (F4-T2), OBO to ELN (F4-T4), the Temporal
  mTLS + HPC identity bridges (F4-T6) — need live Entra/tenant + Temporal. Also remaining: making
  `requested_by` a *required* Entra oid across all workflow inputs, and per-request
  role→`RoleFilteredSkillsSource` scoping (needs a per-user agent or an ambient skills filter).
