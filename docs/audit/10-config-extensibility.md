# 10 — Configuration & Extensibility: Investigation & Design

**Scope:** how a developer/operator adds each of the five extensible things — **skills, MCP
servers, tools, datasources (incl. a datasource's *type* and its type-specific nature), and
per-use-case agent workflows** — and whether the configuration substrate under them
(`chemclaw/config.py`) is the right foundation. Analysis + throwaway feasibility spikes;
correctness/security bugs from prior audits (08/09, D-053/D-065/D-072) are out of scope.
**Date:** 2026-07-24
**Method:** four parallel code sweeps (config/registries, datasource seam, agent/workflow
layer, cross-cutting safety), external benchmarking of Python plugin patterns, and three
offline spikes. Every current-state claim is `file:line`-cited and read, not assumed.

---

## 0. Executive summary

Chemclaw3 is a **single-repo, in-tree** application. It already has the right instincts —
`{name: factory}` registries, filesystem discovery, config tokens — but the five extension
seams are at **wildly different maturity**, and one whole category (per-use-case agent
workflows) does not exist. The substrate is one flat `pydantic-settings` singleton
(`chemclaw/config.py`, ~1062 lines, 18 mixins). Challenging it against external plugin
frameworks (entry-points, pluggy, Django apps) confirms we should **not** adopt any of them
— they solve the *out-of-tree / third-party distribution* problem we don't have. The correct
evolution is **additive and idiom-preserving**: typed pydantic discriminated unions for
type-varying config, a light validated per-extension manifest formalizing the discovery we
already do, and a thin `@register` decorator + `AgentProfile` seam.

**Maturity of the five seams today (falsified against the code):**

| Seam | Mechanism (cited below) | "Add one" cost today | Verdict |
|---|---|---|---|
| **Skills** | `FileSkillsSource(settings.skills_dirs)` + `RoleScopedSkillsSource` gate; `skills/<name>/SKILL.md` | drop a folder, no code | **Good** |
| **MCP servers** | config-as-registry `mcp_servers: list[McpServerSpec]` → `_mcp_capability_tools()` | one JSON/env entry, no code | **Good** |
| **DataSources** | `{name: factory}` registry `sources/registry.py` + `data_sources` token; `DataSource` composes two halves | 1 adapter + 1 registry entry + 1 token | **Good structurally; no per-*type* config** |
| **Tools** | plain async fns in a **hardcoded** `_capability_tools()` list | edit a Python list | **Weakest** |
| **Use-case agent workflows** | **none** — one global `build_agent`, static worker lists, zero front-door routing | not possible without core edits | **Absent (biggest gap)** |

**Incidental blocking finding:** `.env.example` contains an **unresolved Git merge conflict**
(markers at lines **156 / 170 / 173**, around the embedding-retrieval vs `RETRIEVAL_DEFAULT_CONFIDENCE`
block). This is out of scope to fix here, but it should be reconciled promptly — a committed
conflict marker breaks anyone copying `.env.example` to `.env`.

---

## 1. Current-state map (per-seam call-path traces)

### 1.1 The config substrate — one flat typed singleton
`chemclaw/config.py`: a flat `Settings(BaseSettings)` composed from **18 per-domain mixins**
(`config.py:1021-1040`), each owning its section's fields, `@model_validator(mode="after")`
cross-field guards, and derived properties. Load contract (`config.py:1052-1057`):
`env_prefix="CHEMCLAW_"`, `env_file=".env"`, `extra="forbid"`. A module-level singleton
`settings = Settings()` (`config.py:1060`) is imported directly in **~128 files** — there is no
DI container; the singleton *is* the injection mechanism. Complex fields use two idioms:
**typed JSON** (`mcp_servers: list[McpServerSpec]`, `model_routes`/`*_role_gates` dicts) and
**delimited-string + derived property** (`skills_dir`→`skills_dirs`, `data_sources`→`data_source_list`,
`entra_expensive_actions` frozenset). In-cluster, Helm renders `.Values.config` into a ConfigMap
of `CHEMCLAW_*` keys (`deploy/helm/chemclaw/templates/config.yaml`, `_helpers.tpl`) → pod env →
the same singleton. No second config system exists in-cluster.

### 1.2 Skills — filesystem discovery (Good)
`build_agent` wires `SkillsProvider(RoleScopedSkillsSource(FileSkillsSource(settings.skills_dirs),
settings.skill_role_gates))` (`agents/chemclaw_agent.py:120-122`). `FileSkillsSource` walks the
pathsep-split `skills_dir` (`config.py:484-492`, default `"skills"`), reading `skills/<name>/SKILL.md`
frontmatter (name/description) + body with progressive disclosure. `RoleScopedSkillsSource`
(`agents/skill_access.py:24-51`) hides a gated skill from callers lacking its roles (ambient
identity via `agents.identity_context`). Validation gate: `scripts/validate_skills.py`
(`make skill-validate`) requires name/description and name==dir. **Add one:** create a folder.

### 1.3 MCP servers — config-as-registry (Good)
`McpServerSpec` (`config.py:35-46`: name/command/args/allowed_tools) in
`mcp_servers: list[McpServerSpec]` (`config.py:434-447`, two defaults). `_mcp_capability_tools()`
→ `_mcp_tool(spec)` builds one `MCPStdioTool` per spec (`agents/chemclaw_agent.py:244-264`);
`allowed_tools` keeps write/index tools off the agent. **Add one:** append a spec (JSON via
`CHEMCLAW_MCP_SERVERS`), no `build_agent` change.

### 1.4 DataSources — the flagship registry (Good structurally)
`sources/base.py`: `DataSource` is a `runtime_checkable` Protocol with `name`/`ingest`/`retrieve`
(`base.py:27-48`); `SourceSpec` composes an optional `IngestHalf = ElnAdapter` and
`RetrieveHalf = SourceRetriever` (`base.py:20-21,51-66`), rejecting a source with neither half.
`sources/registry.py:22-36`: `DATA_SOURCES: dict[str, Callable[[], DataSource]]` (graph/vector/
lexical/eln-json/eln-ord). `make_data_source(name)` (`:39-45`) resolves; `active_ingest_sources()`
/`active_ingest_source_names()`/`active_retrieve_sources()` (`:53-69`) build the set named in
`settings.data_source_list`. Three consumers iterate those helpers and never hardcode a source
(`agents/research_tools.py`, `workflows/eln_sync.py`, `workflows/memory_jobs.py`). **Add one:**
one adapter + one registry entry + one config token. (ADR D-050/D-053; the old `eln/registry.py`
was consolidated here.)

### 1.5 Tools — the hardcoded list (Weakest)
`_capability_tools()` (`agents/chemclaw_agent.py:222-241`) is a **static Python list** mixing
in-process async fns (`compute_xtb_energy`, `find_notes`, `gather_evidence`, `propose_knowledge_note`,
…) with `*_mcp_capability_tools()`. No registry, no decorator. Two middlewares wrap every tool
uniformly — `make_audit_middleware` then `enforce_tool_authz` (`chemclaw_agent.py:124-133`,
`middleware=[audit, enforce_tool_authz]`), audit outermost so a denied call is still recorded.
**Add one:** write the fn *and edit the list*. This is the only seam whose extension requires
touching orchestration code.

### 1.6 Use-case agent workflows — absent (biggest gap)
One global agent: `build_agent` hardcodes `_INSTRUCTIONS` (`:50`), `_capability_tools()` (`:222`),
skills, MCP, middleware, and branches on the **global** `settings.harness_enabled` (`:140`). The
front door builds exactly one agent per process (`service/app.py:135-169`, `_agent()` at `:230`);
`MessageIn` carries only `message`; `run_turn` (`service/runner.py:46`) never inspects any use-case
field. Which Temporal workflow runs is an emergent LLM tool-choice (`agents/qm_tools.py` →
`client.start_workflow`), not routing. Workers register workflows via **hand-maintained module
lists** (`workers/hpc_worker.py:31-37`, `workers/background_worker.py:62-94`), not a registry.
**There is no seam to add a per-use-case agent configuration.**

### 1.7 Precedent registries to imitate
`evals/metric.py:69-99` (`_REGISTRY` + `@metric` decorator + `get_metric` raising valid keys) ·
`bo/objectives.py:90-101` (`{name: factory}`) · `sources/registry.py` (registry + config token) ·
discriminated union `bo/problem.py:57` (`Field(discriminator="kind")`). These four shapes are the
whole vocabulary the improvements below reuse — **no new pattern is introduced.**

---

## 2. Friction ranking (what's hard/inconsistent/unsafe today)

1. **Tools require core edits (highest friction).** Adding a capability means editing
   `_capability_tools()` — the one seam that forces an orchestration-file change, and the only
   category with no registry despite three registry precedents already in-repo.
2. **No per-use-case agent configuration (highest *absence*).** Every dimension a use case would
   vary (instructions, tool subset, MCP subset, harness mode) is already a `build_agent` input
   drawn from globals, but there is no way to bind them into a named, selectable bundle.
3. **DataSource *type* has no first-class config.** A "type" is just a registry key bound to a
   factory that privately reads flat globals (`JsonExportAdapter` reads `settings.eln_export_dir`,
   `eln/json_adapter.py:97-99`). There is **one** `eln_export_dir` regardless of how many JSON
   sources you configure — so two instances of one type (prod + staging Snowflake with different
   warehouses) are impossible, and a type's required fields (connection/credential/mapping/cursor)
   are undeclared and unvalidated.
4. **Config idiom inconsistency.** Some complex fields are typed JSON (`mcp_servers`), others are
   delimited strings with a parsing property (`data_sources`, `skills_dir`). Fine, but it means
   "how do I configure a list?" has two answers depending on the field.
5. **Discovery ≠ enablement is only half-modeled.** Skills are discovered by folder but there is
   no *explicit enabled/ordered list* — every discovered skill is active (subject only to role
   gates). Datasources have the opposite: an explicit token but no discovery.
6. **`.env.example` merge conflict** (lines 156/170/173) — a committed conflict marker.

---

## 3. External benchmark verdict (challenging the substrate)

Benchmarked against Python's established extension patterns. The load-bearing distinction:
**in-repo extension** (our case — we own the registry) vs **out-of-tree/third-party plugins**
(separate distributions we didn't build). Almost every framework exists for the second problem.

| Pattern | Fit for a single-repo app | Verdict |
|---|---|---|
| `importlib.metadata` **entry-points** | buys cross-distribution discovery we don't need; a parallel registry to our own code | **Reject** (revisit only if third parties will `pip install` plugins) |
| **pluggy** (pytest/tox) | built for 1:N hook fan-out over a broad surface; our points are 1:1 name→impl lookups a dict already models | **Reject** |
| Django `INSTALLED_APPS`/`AppConfig` | machinery too heavy, but two ideas are worth stealing | **Steal, don't adopt:** an explicit *enabled/ordered list* in config, and a per-extension `ready()`-style init hook (~10 lines) |
| **pydantic-settings** nested models + **discriminated unions** + custom sources | an evolution of what we already run; unions directly model "a `type` tag + type-specific fields" | **Adopt (best fit)** |
| **Per-plugin manifest** (TOML/YAML beside code, globbed + merged) | exactly our `SKILL.md` shape; metadata-without-import; PR-gate-friendly | **Adopt, lightly** (formalize discovery; keep enablement in central config) |
| MCP client-config convention (stdio vs HTTP `type`) | a textbook discriminated union; the hosted MCP *registry* is out-of-scope for in-repo servers | **Adopt the config shape; reject the hosted registry** |
| LangChain/LlamaIndex `@tool` decorator + list | validates our instinct: decorator-populates-a-dict, metadata from docstring/type-hints | **Adopt the sugar** (`@register`) |

**Net:** evolve the substrate you have (typed sections + discriminated unions), standardize
discovery (per-folder manifest + explicit enable-list + init hook), and add a `@register`
decorator for tools. Introduce **no** plugin framework — each surveyed one targets the
out-of-tree problem Chemclaw3 doesn't have, which is precisely the repo's "no abstraction
without a second caller" line.

---

## 4. Options matrix per seam (scored)

Scoring axes: **Ease** (of adding one) · **Safety** (RBAC + GxP audit + PR-gate + fail-fast
validation preserved) · **KISS** (Rule-of-Three / no speculative abstraction) · **Blast radius**.

| Seam | Option | Ease | Safety | KISS | Blast | Pick |
|---|---|---|---|---|---|---|
| **Tools** | keep hardcoded list | low | ok | ok | — | |
| | `@tool` decorator + registry (mirror `@metric`) | **high** | **high** | **high** | small | ✅ |
| **DataSource type** | keep flat-fields-per-instance | high | med | high | none | (file sources) |
| | `DataSourceSpec` discriminated union (scoped) | **high** | **high** | **high** | small | ✅ (config-carrying sources) |
| | separate `SourceType` registry layer | med | high | low | med | ✗ over-built (Rule of Three unmet) |
| **Use-case workflows** | in-code `AgentProfile` registry (staged) | **high** | **high** | **high** | small | ✅ |
| | filesystem-discovered profiles | med | high | med | larger | defer (needs 2nd profile + content authoring) |
| | profile = skill-bundle manifest | med | med | low | med | ✗ overloads skills layer |
| **Skills** | keep filesystem discovery | high | high | high | — | ✅ (add manifest fields + enable-list) |
| **MCP servers** | keep config list | high | high | high | — | ✅ (add stdio/HTTP `type` union) |
| **Substrate** | keep flat singleton | — | — | high | — | ✅ evolve additively (nested/union), don't replace |

The **safety column is non-negotiable** and is structurally satisfied by every ✅ option because
the audit + authz middleware (`chemclaw_agent.py:133`) and skill role-gates run *after* any
registry/profile narrowing — see the invariant in §7.

---

## 5. DataSource-*type* design (worked through Snowflake) — recommended

**Recommend a scoped discriminated union.** Add a `DataSourceSpec` pydantic discriminated union
(on a `type` field) for sources that carry per-instance config, introduced **additively**
alongside the existing comma-string `data_sources` (keep the bare-key UX for graph/eln-json — no
regression). Each variant nests its own config; the registry dispatches `type → factory(spec)`.

Why this and not the alternatives: it is the **faithful generalization** the repo's own ADRs
endorse (D-054 generalizes a contract only as far as an existing concrete case forces), it
**reuses two in-repo idioms** (`McpServerSpec` typed list + `bo/problem.py:57` discriminator), and
it keeps the three consumers untouched (they iterate built sources, never specs). A separate
`SourceType` registry layer is the "universal abstraction" `DEFERRED.md` reserves for the third
real source — Rule of Three unmet, rejected.

**Snowflake plugs in as:** one `SnowflakeSourceSpec` variant nesting `SnowflakeConnection`
(account/warehouse/database/schema/role), `SnowflakeAuth` (`mode: keypair | entra_obo` + a
`credential_ref` or an OBO `scope`), and `SchemaMapping` (`reaction_table`, `cursor_column`,
`smiles_column`); one registry factory entry; one list token. Its load-timestamp cursor stays a
**datetime**, so it reuses `load_cursor(name)` unchanged (D-054's per-source cursor). OBO is the
generic dormant seam `agents/identity/obo.py::exchange_obo` (`entra_obo_enabled`, `config.py:662`)
gaining its **first real caller**. Two instances (prod + staging) with different warehouses
coexist because config is per-spec, not global — the exact capability flat-fields cannot provide.

**Keep the Temporal boundary string-keyed:** `sync_eln_entries(source: str, …)` (`workflows/eln_sync.py:138`)
should keep passing the source *name*; the registry resolves name→spec internally, so in-flight
workflow histories stay byte-identical (durability > signature elegance).

**Proven by Spike 3** (below): a single JSON list token carrying real connection/auth/mapping
config round-trips, the discriminator dispatches, per-variant validation rejects a field that
doesn't belong to the chosen type, the built source satisfies the `name`/`ingest`/`retrieve`
shape the consumers iterate, and two instances with distinct warehouses build cleanly.

---

## 6. Use-case-workflow design (AgentProfile) — recommended

**Recommend a named `AgentProfile` seam, staged.** A profile is an override-bundle over
`build_agent`'s existing dimensions: `instructions + tool subset + MCP subset + (later) skill
subset + harness mode/autonomy`, selectable per session, defaulting **byte-for-byte** to today's
agent. It is *not* a Temporal workflow (no durability, no step sequencing — so it cannot become a
backdoor to the deliberately-unbuilt MAF graph-workflows, `docs/harness-konzept.md §11`), *not* a
new agent mode (harness is orthogonal — it becomes one *dimension* a profile sets), and *not* a
skill bundle (that would overload the judgment layer).

**Staging (honors Rule of Three — only ONE use case exists today):**
- **Stage 1:** `agents/profiles.py` — `AgentProfile` (pydantic, like `McpServerSpec`) + a one-entry
  `{name: profile}` registry (mirroring `sources/registry.py`/`bo/objectives.py`) whose sole
  `"default"` entry reproduces today's agent; `build_agent(profile=…)` resolves it (None→default),
  filters `_capability_tools()` and `settings.mcp_servers` by the profile's name-subsets, and
  picks instructions + harness flags. Provable offline with no front-door change.
- **Stage 2 (when a 2nd use case lands):** add `profile` to `POST /sessions`, carry it through
  `_LiveSessions`/`SessionOwners`, cache one agent per profile name in `app.state.agents`, add a
  `profile` config token + optional profile-name RBAC gate.
- **Stage 3 (only under authoring pressure):** filesystem-discovered `profiles/<name>/profile.md`
  on top of the same registry.

**Invariant — a profile *attenuates*, it does not *authorize*.** Because `enforce_tool_authz` and
`RoleScopedSkillsSource` run **regardless of profile**, a profile that lists a tool the caller may
not use is still denied at call time, and a profile that omits the PR-gate tools merely removes
capability. A profile is a narrowing seam layered *under* RBAC, never a bypass. **Proven by
Spike 2.**

---

## 7. The safety rubric every new seam must preserve

Any registry/decorator/profile/type change MUST keep these four, which today are structural, not
per-call opt-ins:
1. **GxP audit** — `make_audit_middleware` wraps every tool (`chemclaw_agent.py:124-133`); a new
   tool sourced from a registry is wrapped identically (audit is applied to the *assembled list*,
   not per-definition). **Spike 1** shows a registry-sourced tool is still audited, including a
   denied call.
2. **Per-tool authz** — `enforce_tool_authz` (`agents/authz.py` `tool_role_gates`) runs after
   audit; **Spike 1 & 2** show narrowing-then-gating (a profile/registry cannot widen authority).
3. **PR-gate** — write/index paths go through `kg/pr_gate.py`, and the PR-gate lives *inside* the
   tools (`propose_knowledge_note`, `record_confirmed_answer`); omitting a tool from a profile or
   registry removes capability but cannot weaken the gate.
4. **Fail-fast validation** — cross-field `@model_validator`s at startup (`config.py`); a new
   typed spec (`DataSourceSpec`, `AgentProfile`) gets pydantic per-variant validation for free and
   should add a `make validate-*` gate mirroring `make skill-validate`.

---

## 8. Spike verdicts (throwaway feasibility — code discarded)

All three run offline against a minimal `pydantic`/`pydantic-settings` venv (no MAF/rdkit/bofire),
because each proves a **config/registry pattern**, not scientific behavior. All three **PASS**.

- **Spike 1 — `@tool` registry (de-risks the weakest seam).** A `@tool(name)` decorator +
  `_TOOL_REGISTRY` (exact `@metric` shape) assembles the advertised list; a `agent_facing=False`
  flag keeps write/index tools off the agent (the `allowed_tools`/PR-gate exclusion). Wrapping the
  registry-sourced list with the audit+authz chain shows: every tool audited, authz still denies,
  a denied call is still audited, hidden tools excluded. **Verdict: feasible; the real change is
  mechanical** (decorate the existing async fns; build the list from the registry).
- **Spike 2 — `AgentProfile` seam (de-risks the biggest gap).** Default profile == today's agent
  (full toolset, base instructions, audit+authz present); a use-case profile narrows tools, swaps
  instructions, flips harness — via one registry entry; narrowing doesn't mutate the default; and
  the *attenuate-not-authorize* invariant holds (a permissive profile still can't bypass authz).
  Unknown profile fails loud with valid keys. **Verdict: feasible without any front-door rework to
  prove selection; §6 staging is safe.**
- **Spike 3 — `DataSourceSpec` discriminated union (de-risks datasource *type*).** A single JSON
  list token with real Snowflake connection/auth/mapping config round-trips and dispatches by
  `type`; two instances with distinct warehouses coexist; a keyless file source still validates
  from a bare spec (no regression); a field foreign to the chosen variant is rejected; the
  consumer helper sees only names/halves (spec shape never leaks); the cursor stays a name-keyed
  datetime. **Verdict: feasible; the "one variant + one token" story survives real connection
  config.**

---

## 9. Recommendation — prioritized, dependency-ordered backlog

Each is an ADR-ready candidate `BACKLOG.md` item with a trigger and a rough size. Ordered by
value/effort; none requires a live tenant/cluster.

1. **[S] Fix `.env.example` merge conflict** (lines 156/170/173). *Trigger: now* — a committed
   conflict marker breaks `.env` copying. (Not done here — flagged only, per scope.)
2. **[M] Tool registry (`@tool` + `_TOOL_REGISTRY`).** Mirror `evals/metric.py`; build
   `_capability_tools()` from the registry; keep `agent_facing`/allow-list; audit+authz unchanged.
   *Highest friction removed; Spike 1 proves it.* Add a `make tool-validate` gate.
3. **[M] `AgentProfile` seam, Stage 1.** `agents/profiles.py` + one `"default"` entry +
   `build_agent(profile=…)` narrowing. *Ship the seam, not speculative profiles; Spike 2 proves it.*
   Stage 2 (front-door selection) triggers when a **second use case** appears.
4. **[M] `DataSourceSpec` discriminated union (scoped), Stage 1.** Add the union + a near-empty
   variant for existing keyless sources, keep the comma-string token, keep the Temporal boundary
   string-keyed. *Trigger: the Snowflake connector (already deferred) — Spike 3 proves the shape.*
   The Snowflake adapter itself is the first `exchange_obo` caller.
5. **[S] Per-extension manifest + explicit enable-list (steal from Django/#5).** Formalize
   `SKILL.md`-style frontmatter with a pydantic-validated manifest (declare tool/MCP deps,
   use-case tags) and keep enablement in central config (discovery ≠ auto-enable). *Trigger: when
   skills need to declare capability deps, or profiles (Stage 3) need content-only authoring.*
6. **[S] MCP transport `type` union.** Add a stdio/HTTP discriminator to `McpServerSpec` so remote
   MCP servers configure cleanly. *Trigger: first remote/HTTP MCP server.*
7. **[S] Config idiom convergence (doc, not churn).** Document "typed JSON list for config-carrying
   collections, delimited-string for bare-key sets" as the house rule; don't migrate existing
   fields (speculative churn). *Trigger: next new collection field.*

**One-pattern-or-many?** Converge on **one vocabulary** — registry (`{name: factory/spec}`) +
optional discriminated union for type-varying variants + optional filesystem discovery + a config
enable-token — applied per seam at the depth each needs. Do **not** force a single uniform seam
(a bare-key file source shouldn't pay the JSON-spec tax). This matches how the repo already runs
`entra_expensive_actions` (string) and `mcp_servers` (typed list) side by side.

---

## Appendix — critical files

- Substrate: `chemclaw/config.py` (mixins, `McpServerSpec:35`, validators, singleton:1060),
  `.env.example`, `deploy/helm/chemclaw/values.yaml` + `templates/config.yaml`
- Skills: `agents/chemclaw_agent.py:120`, `agents/skill_access.py:24`, `scripts/validate_skills.py`
- MCP: `chemclaw/config.py:434`, `agents/chemclaw_agent.py:244-264`, `mcp_servers/*/server.py`
- Tools: `agents/chemclaw_agent.py:222-241` (`_capability_tools`), `agents/*_tools.py`, `agents/tool_authz.py`
- DataSources: `sources/base.py`, `sources/registry.py`, `eln/adapter.py`, `eln/json_adapter.py:97`,
  `workflows/eln_sync.py:138`, `tests/test_datasource_seam.py`
- Use-case workflows: `agents/chemclaw_agent.py` (`build_agent`/`_INSTRUCTIONS`), `service/app.py:135`,
  `service/runner.py:46`, `docs/harness-konzept.md §11`
- Cross-cutting: `agents/authz.py`, `agents/audit.py`, `kg/pr_gate.py`
- Precedent registries: `evals/metric.py:69-99`, `bo/objectives.py:90-101`, `bo/problem.py:57`
- Spikes (throwaway, in the session scratchpad, not committed): `spike1_tool_registry.py`,
  `spike2_agent_profile.py`, `spike3_datasource_type.py` — all PASS offline.
