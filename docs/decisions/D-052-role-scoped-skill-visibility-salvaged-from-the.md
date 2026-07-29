# D-052 — Role-scoped skill visibility (salvaged from the phase6-authz branch)

**Context.** F4 (D-043…D-047) landed real Entra identity + RBAC — token validation, the
`require_actor` reject-if-absent rule, and `authorize_trigger` gating expensive *actions* by role —
but left **skill visibility** as a dead placeholder: `RoleFilteredSkillsSource` filtered by an
`allowed_skills` name-set that **no caller ever computed**. A parallel `phase6-authz` line of work
had independently built a better skill-scoping mechanism (plus a duplicate `Principal` and a second,
competing tool-authorization path). Per the instruction to keep only the better code, this salvages
the one genuinely-superior, non-redundant piece and discards the rest.

**Decision.**
- `agents/skill_access.py`: `RoleFilteredSkillsSource` → `RoleScopedSkillsSource` — a config-driven
  gate (`settings.skill_role_gates`: skill name → allowed roles). Ungated skills stay visible to all
  (empty map = today's behavior); a gated skill is hidden from a caller holding none of its roles.
  Roles are read from the turn's **ambient identity** (`agents.identity_context.get_current_roles`,
  the same source `audit`/`authz` read) rather than threaded through `build_agent`, so it composes
  with the landed F4 flow instead of introducing a second identity object.
- `build_agent` drops the unused `allowed_skills` param and wires the gate from config.
- `chemclaw/config.py`: `skill_role_gates` (JSON-overridable) + `.env.example`.

**Deliberately dropped from that branch (already implemented better by F4, so not merged):** its
`chemclaw/identity.py::Principal` (F4's `service/auth.py::Principal` does real JWT validation), its
`agents/authz.py` + `tool_role_gates` (F4's `require_actor`/`authorize_trigger` is the landed
action-authz — a second mechanism would violate DRY), and the `security-posture-note` branch's
"no authn/authz yet" documentation, whose premise F4 has superseded.

**Result.** `make lint type test` green; `mypy --strict` clean. `tests/test_skill_access.py`
rewritten for the ambient-roles design (no gates = all visible; gated skill hidden from an anonymous
turn and from a role-lacking caller, shown to one holding the role; ungated skills unaffected).
