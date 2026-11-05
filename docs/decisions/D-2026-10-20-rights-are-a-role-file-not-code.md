# D-2026-10-20-rights-are-a-role-file-not-code — which role may use which tool, datasource and profile is one YAML file

**Status:** accepted · **Date:** 2026-10-20

## Context

Authorization was spread over four settings (`tool_role_gates`, `tool_authz_default`,
`entra_privileged_roles`, `skill_role_gates`) plus a `required_roles` field on one datasource type.
Every datasource except the mounted share was open to any authenticated user, a session could be
opened on any profile, and there was no way to say "this role answers only". Adding a role meant
editing four lists, and a typo in any of them was an ungated tool.

## Options

1. **Extend the existing lists** — add role lists per source and per profile. Four more places to
   keep consistent, and still no capability tier.
2. **One policy file keyed by role** — each role block grants tools (by tier, bundle or name),
   datasources and profiles, and may carry the reviewer acts. Adding a role is adding a block.
3. **Roles in code** — an enum of the four roles. Rejected: every new role is a release.

## Decision

Option 2. `core/access_policy.py` loads `access_policy_file` (shipped example
`data/access/roles.yaml`). A person's rights are the union over the roles they hold; a token with
none of the listed roles holds nothing. `deny` subtracts only from its own block. The tiers are
derived, not listed: `tier:write` is `authz.side_effecting_call`, `tier:read` is its complement, so
a new connector tool is classified the day it is enabled. The policy is enforced where each thing
already was decided: `authorize_tool` (and so every tool call), the retrieval fan-out (a source the
roles do not open is a declared skip, never "no precedent"), session creation and `/profiles`, and
the reviewer check. Unset, nothing changes. Set, the policy replaces the privileged-role checks.
A name in the policy that the deployment does not have refuses startup.

## Consequences

- Durable activities bind no roles (D-2026-08-28), so under a policy a background evidence sweep
  asks no source. Restoring it needs the signed identity payload already in the backlog.
- The plan gate still applies on top of `tier:write`.
- Tools are still advertised to every role and refused at call time; hiding them is a separate
  change.
- Held by `tests/test_access_policy.py`, which also checks the shipped file against the deployment.

Revisit when: a role needs rights narrower than a datasource (rows by project or compound).
