# D-068 — Write tools are role-gated by default (DEFAULT_WRITE_TOOL_GATES)

**Context.** Per-tool RBAC defaulted to `tool_authz_default="allow"`: any tool without an explicit
`tool_role_gates` entry — including job launchers and state-mutating tools — was callable by every
authenticated user. Flipping the global default to `deny` would break the dev flow and every read
tool.

**Decision.** `agents/authz.py` gains `DEFAULT_WRITE_TOOL_GATES`, the built-in set of
write/side-effect tools (`submit_qm_job`, `propose_knowledge_note`, `record_confirmed_answer`,
plus `index_molecule`/`index_reaction` as defense-in-depth behind the D-029 `allowed_tools`
boundary). Under `entra_required`, an *unconfigured* tool in this set requires a role from
`entra_privileged_role_set` — reusing the F4-T5 privileged set rather than inventing a second role
vocabulary — and fails closed when that set is empty. An explicit `tool_role_gates` entry
overrides the built-in gate; read tools keep the `allow` default; dev mode is unchanged. The
constant lives in `authz.py`, the one home for authorization decisions.

**Consequence.** Secure by default: an enforced deployment can no longer expose writes by
forgetting to configure a gate. A new write tool must be added to the set when registered — a
hand-maintained list, acceptable at the current tool count.

**Result.** `tests/test_tool_authz.py` proves: default-gated write denied/allowed by privileged
role, fail-closed on an empty privileged set, operator override wins, read tools and dev mode
unchanged.
