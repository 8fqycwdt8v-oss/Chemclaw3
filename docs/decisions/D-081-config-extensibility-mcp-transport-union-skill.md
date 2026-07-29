# D-081 — Config-extensibility: MCP transport union, skill manifest + enable-list, config idiom rule (audit doc 10, items 5–7)

**Context.** The last three items of `docs/audit/10-config-extensibility.md` §9. Each was
trigger-gated in BACKLOG; the triggers were waived deliberately (see "Rule of Three" below).

**Decision 1 — MCP transport union (item 6).** `McpServerSpec` became
`StdioMcpServerSpec | HttpMcpServerSpec` discriminated on `transport`; `_mcp_tool` dispatches to
`MCPStdioTool` or MAF's `MCPStreamableHTTPTool` and is `assert_never`-exhaustive. A remote server
is now config, not a code edit — the same friction the tool registry removed for tools.

*Backwards compatibility is the load-bearing design point.* Every config written before this — 
`.env.example`, Helm values, any deployment's `CHEMCLAW_MCP_SERVERS` JSON — carries no `transport`
key, and a plain `Field(discriminator=…)` rejects an untagged payload outright, breaking every
existing deployment at startup. So the union uses a **callable** `Discriminator` that reads a
missing tag as `"stdio"` (the only transport that existed then), with `Tag(...)` on each member.
New servers tag themselves explicitly; old configs are untouched. The public name `McpServerSpec`
is kept for the union, so every existing annotation and import stays valid.

**Decision 2 — skill manifest + enable-list (item 5).** Two halves of audit friction #5
("discovery ≠ enablement is only half-modeled"):
1. `agents/skill_manifest.py` — `SkillManifest`, the `SKILL.md` frontmatter as a pydantic contract
   (`name`/`description` required, optional `tools`/`mcp_servers`/`tags`, `extra="forbid"`).
   `make skill-validate` now validates against it **and checks the declared capabilities against
   the live registries** (`agents.tool_registry`, `settings.mcp_servers`). That check is the real
   payoff and is only possible because of D-075's tool registry: a skill still teaching a renamed
   or deleted tool now fails CI instead of surviving as plausible, stale prose. Four shipped skills
   declare their real deps, so the mechanism has actual callers, not a speculative schema.
2. `EnabledSkillsSource` + `settings.skills_enabled` — an explicit enable-list, so a deployment can
   ship the whole skills tree and advertise the validated subset without deleting folders. Empty
   (the default) means every discovered skill: a no-op until opted into.

**Invariant preserved — both narrowings *attenuate*, neither authorizes.** The enable-list cannot
advertise a skill no directory provides, and `RoleScopedSkillsSource` still runs on top of it, so
enablement is layered *under* RBAC exactly as a profile is (D-075). A manifest's declared tools are
**documentation the gate validates, never a grant**: what the agent may call is decided by the
registry/profile and `enforce_tool_authz`, which this seam does not touch.

**Fail-fast, placed where it belongs.** An unknown name in `skills_enabled` is reported by
`make skill-validate`, not raised by `EnabledSkillsSource` — the source runs per turn, so a config
typo must degrade the advertised set rather than break every live conversation. The loud failure
belongs in the pre-deploy gate; the runtime stays resilient.

**Decision 3 — config idiom house rule (item 7).** Recorded in `config.py`'s module docstring
(where anyone adding a field reads it): *typed JSON list when elements carry their own config
(discriminate when they vary by kind); delimited string when elements are bare keys resolved
against a registry, exposed via a derived `*_list` property.* Existing fields are **not** migrated
— that would be churn without a defect. Documented, per the audit, as "doc, not churn".

**Rule of Three note.** Items 5 and 6 were BACKLOG-gated on triggers (a first remote MCP server; a
skill needing to declare deps) that had not fired; they were built on explicit instruction to
complete the backlog. Both are honest rather than speculative: item 6 is a real second variant with
a working dispatch, and item 5's dependency check has four real declaring skills today. The parts
that would have been speculative stayed out — no HTTP server is configured, and profile Stage 3
(filesystem-discovered profiles) is still deferred.
