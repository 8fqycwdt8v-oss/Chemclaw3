# D-060 — F10-C: per-tool authorization middleware (supersedes D-044 scope, D-A12)

**Context.** `authorize_trigger` guarded only the expensive `submit_qm_job` trigger (F4-T5). Tool-use
governance at *every* invocation was a platform delta.

**Decision.** `agents/tool_authz.py::enforce_tool_authz` is a MAF `@function_middleware` (same shape
as the audit middleware) that calls `agents/authz.py::authorize_tool(tool)` before each tool runs,
gating on `settings.tool_role_gates` (JSON tool→roles) with `tool_authz_default` (`allow`|`deny`).
`authorize_tool` and `authorize_trigger` share one `_has_required_role` predicate (DRY). Enforcement
is active only under `entra_required`; the expensive-trigger call stays as defense-in-depth.

**Consequence.** Default `allow` + empty gates = zero behavior change; a deployment opts into an
allowlist by config. Authorization is now uniform per tool call, superseding D-044's trigger-only
scope.

**Result.** `make lint type test` green. Tests: `test_tool_authz`, `test_agent` (two middlewares),
`test_config`.
