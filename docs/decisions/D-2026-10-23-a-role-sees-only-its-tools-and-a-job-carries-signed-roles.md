# D-2026-10-23-a-role-sees-only-its-tools-and-a-job-carries-signed-roles — hide what a role cannot call; sign what a job inherits

**Status:** accepted · **Date:** 2026-10-23

## Context

`D-2026-10-20-rights-are-a-role-file-not-code` refused a tool at call time but still advertised it,
so the prompt, the skills and the schemas described capabilities the account could not use, and the
model planned around tools it would be refused. It also left background report sweeps with no
roles: a workflow payload is relayed data, so the activity bound the empty set and a policy-gated
deployment's sweeps asked no datasource.

## Options

1. **Filter the tool surface at graph build; sign the roles at launch.** The graph is compiled per
   turn inside the turn's ambient identity, so the roles are in hand. The API HMAC-signs actor and
   roles with a shared key; the activity binds the roles only if the signature verifies.
2. **Refuse only at call time; trust `requested_roles` in the payload.** No new setting, but any
   writer of workflow history could grant itself a role.
3. **Mint a token for the worker (on-behalf-of).** Strongest, but a token expires inside a long job
   and every worker would need Entra reachability.

## Decision

Option 1. A first-party or connector tool is bound only if `authz.tool_is_offered` holds it, asked
with no arguments (the filesystem verbs are not bound here, so nothing arguments could change is
hidden on a guess). Call-time authorization is unchanged and still the authority. A report request
carries `roles_claim = sign_roles(actor, roles)`; `retrieve_section` binds `verified_roles(...)`.
No key, no claim, or a claim for another actor or role set yields no roles, as before.

## Consequences

- An account with no listed role is bound no tools at all.
- `CHEMCLAW_ROLE_CLAIM_SECRET` must be the same on the API and every worker; unset keeps the previous
  behaviour. The claim has no expiry and is bound to the actor, so it lives as long as its job.
- Only report sections bind verified roles; other durable paths keep their own handling.
- Held by `tests/test_role_claim.py`.

Revisit when: a job outlives a role change that must take effect mid-run.
