# Sweep: config-driven dynamic resolution

Lens: cross-cutting. Every path where a string in a YAML/Markdown file becomes a Python object,
plus every deserializer that could execute code. Everything below was run in this environment
(`uv run`, repo at `/home/user/Chemclaw3`); outputs are quoted verbatim.

## Inventory of the resolution paths (what each string may name)

| Site | String comes from | Pattern constraint | Resolves to | Resolved when |
|---|---|---|---|---|
| `connectors/jobs.py:89 resolve_params_model` | `connector.yaml` `jobs[].params_model` | `^[\w.]+:[A-Za-z_]\w*$` | any importable module + attr; must be a `BaseModel` subclass | agent build (`_capability_tools`) |
| `connectors/jobs.py:118 resolve_precondition` | `connector.yaml` `jobs[].precondition` | same | any importable module + attr; must be `callable` | **job launch only** (see finding 3) |
| `connectors/registry.py:218 server_tools_module` | bundle *directory name* (`^[a-z][a-z0-9-]*$` on the manifest, and the dir must match) | n/a | `chemclaw.connectors.<name>.server.tools` | validators only (`validate_connectors`, `validate_templates`, two tests). Not on any serving path. |
| `ingest/sources/registry.py:113 resolve_half` | `datasource.yaml` `ingest:` / `retrieve:` | **none** | any importable module + attr; must be `callable` | first use of that half |
| `ingest/eln/warehouse/connect.py:38 _resolve_driver` | `datasource.yaml` `config.connection.driver` | only `":" in value` (`binding.py:430`) | any importable module + attr; must be `callable` | first warehouse connection |
| `templates/registry.py:64 _load` | `data/templates/*.yaml` | tool/job/profile names, resolved by *name* against the built surface, not by import | no import | agent build |
| `agent/profile_discovery.py:58 _load` | `data/profiles/*.yaml` + `connectors/*/profiles/*.yaml` | no import | agent config overrides | front-door lifespan |

Every reference shipped in the repo points inside `chemclaw.*` (verified: `grep params_model\|precondition src/chemclaw/connectors/*/connector.yaml`,
and the seven `datasource.yaml` files). Nothing enforces that prefix.

## The realistic trust boundary

**Manifests are not a lower-trust input, but they are also not read-only.** All four manifest roots
are either package-anchored (`connectors_dir`, `data_sources_dir` via `core/config/shipped.py`) or
CWD-relative under `/app` (`templates_dir`, `profiles_dir`), and all of them are inside the image.
Adding a directory requires setting `CHEMCLAW_CONNECTORS_DIR` / `CHEMCLAW_DATA_SOURCES_DIR` etc.,
which is the same privilege as choosing the image — so "an operator can name an arbitrary module"
is not a privilege escalation and I am not reporting it as one.

What *is* worth stating is that the files are writable at runtime by the process that reads them
(finding 7), so any write primitive inside the pod converts to persistent code execution on the
next restart via `precondition`/`params_model`/`ingest`/`driver`. There is no user-facing path that
plants one: attachments are in-memory (`api/routes/sessions.py:146` — "Session-scoped and in-memory
by design"), the skills backend refuses writes outright and runs `virtual_mode=True`, the KG PR-gate
writes Markdown into the knowledge repo (a different tree), and no route or tool writes into a
manifest directory.

## Deserialization audit — clean, and verified rather than read

- **Every `yaml.safe_load` really is `safe_load`.** 8 call sites in `src/` (`profile_discovery.py:58`,
  `templates/registry.py:64`, `connectors/registry.py:138`, `ingest/sources/registry.py:81`,
  `cli/live_probes.py:280`, `evals/live.py:260`, plus tests). `grep -rn "yaml.load\|Loader=\|FullLoader\|UnsafeLoader"`
  over the whole tree excluding `.venv`: **zero hits**.
- **`frontmatter.loads` (KG notes at `kg/note.py:505`, `SKILL.md` at `agent/skill_manifest.py`) is
  SafeLoader.** This one matters because notes are agent-proposed content. Verified by execution:
  ```
  loading '---\n!!python/object/apply:os.system ["touch /tmp/PWNED"]\n---\nbody'
  -> rejected: ConstructorError could not determine a constructor for the tag
     'tag:yaml.org,2002:python/object/apply:os.system'
  PWNED exists: False
  ```
- **`science/calc/thermo.py:115 unpack_npy` — the `allow_pickle=False` comment is true.** Verified:
  a base64 `.npy` holding an object array is rejected with
  `ValueError: Object arrays cannot be loaded when allow_pickle=False`, while a plain float array
  decodes to `(3, 3)`. It is the **only** `np.load` in `src/`; the only sibling is
  `tests/calc_server_fake.py:110`, which is `np.save(..., allow_pickle=False)`. No `pickle`,
  `joblib` or `torch.load` anywhere in `src/`.
- **The LangGraph checkpointer does not pickle.** `JsonPlusSerializer.__init__` signature in the
  installed distribution is `(*, pickle_fallback: bool = False, ...)` and a default instance reports
  `pickle_fallback: False`; `agent/checkpointer.py` passes no `serde`, so checkpoint blobs read back
  out of Postgres go through msgpack/JSON only.
- **No shipped bundle uses the `stdio` transport** (`grep -rl "transport: stdio" src/chemclaw/connectors/`
  → nothing), so the `StdioEndpoint.command`/`args` seam — which is a stronger primitive than
  `importlib`, since it names an executable — is declared and unused today.
- **The skills gate is not bypassable through the composite route prefix.** `NarrowedSkillsBackend._allows`
  takes `parts[0]` as the skill name, which is only correct if `CompositeBackend` strips the route
  prefix before dispatching. Verified by execution: `read('/skills/secret-skill/SKILL.md')` returned
  the refusal string and `permits` was called with `'secret-skill'`, not `'skills'`.

---

## 1. A profile file's `harness_autonomy` is an unvalidated string, so a typo silently turns the plan gate off

- **Severity**: high
- **Location**: `src/chemclaw/agent/profiles.py:55` (`AgentProfile.harness_autonomy: str | None`),
  consumed at `src/chemclaw/agent/plan_gate.py:140 autonomy_for` / `:163 gate_applies`
- **Trigger**: a profile YAML in `data/profiles/` (or in a connector bundle's `profiles/`) containing
  `harness_autonomy: plan-only` — a hyphen instead of an underscore — under a deployment with
  `harness_enabled=true`.
- **Consequence**: `gate_applies()` compares against the constant `PLAN_ONLY = "plan_only"`, gets
  `False`, and `build_langgraph_agent` does not attach the plan gate. Every side-effecting tool
  (every durable job launcher, every `state_changing` connector tool, every `/memories/` write) runs
  without the pre-execution approval the file was written to request. Nothing logs, nothing raises,
  and the profile appears in `GET /profiles` as a normal selectable profile. The deployment-level
  field is `Literal["plan_only", "execute"]` (`core/config/agent.py:142`) so the *same typo in an env
  var* is refused at startup — the file path is the unguarded one, and there is no `profile-validate`
  target in the Makefile, so no CI stage sees it either.
- **Evidence**: three profile files written to `/tmp/prof` and loaded with `CHEMCLAW_PROFILES_DIR=/tmp/prof`:

  ```
  global: plan_only False
  ['honest', 'rogue', 'typo']
  rogue  autonomy= 'Execute'    gate_applies= False
  typo   autonomy= 'plan-only'  gate_applies= False
  honest autonomy= 'plan_only'  gate_applies= True
  ```

  (`rogue.yaml` = `harness_autonomy: Execute`, `typo.yaml` = `harness_autonomy: plan-only`,
  `honest.yaml` = `harness_enabled: true` only.)

  The class docstring at `profiles.py:44` says `extra="forbid"` "rejects a misspelled override rather
  than silently ignoring it (the same fail-fast the config models use)". That is true of misspelled
  *keys* and false of misspelled *values*, which is the half that decides whether a safety gate runs
  — confirmed above, where `description: demo` was rejected outright but `harness_autonomy: plan-only`
  loaded fine.
- **Fix**: give `AgentProfile` the same type the settings field has —
  `harness_autonomy: Literal["plan_only", "execute"] | None = None` — and import the literal from one
  place so the two cannot drift. One line, and it converts every one of these into a startup
  `ProfileError` naming the file.

## 2. A profile file *can* widen: `harness_autonomy: execute` removes the approval gate, contradicting the module's stated invariant

- **Severity**: medium
- **Location**: `src/chemclaw/agent/profiles.py:22` ("A profile *attenuates*, it never *authorizes*"),
  `src/chemclaw/agent/profile_discovery.py:27` ("A file dropped here cannot widen what its caller may
  do"), mechanism at `plan_gate.py:140-166`
- **Trigger**: deployment default `harness_autonomy=plan_only` (the shipped default,
  `core/config/agent.py:142`, described in-comment as "the pharma-safe one"); a profile file declares
  `harness_enabled: true` + `harness_autonomy: execute`; any authenticated caller does
  `POST /sessions {"profile": "<that name>"}` (`api/routes/sessions.py:51-57`, which accepts any
  registered profile name from any principal and exposes the list at `GET /profiles`).
- **Consequence**: that session's agent is built with no plan gate. The two claims above are read by
  reviewers as "a profile file is not a security-relevant artifact"; it is one. The narrowing dials
  (`tool_names`, `mcp_server_names`) genuinely only subtract — I checked `_capability_tools`
  (`chemclaw_agent.py:322-327`, with `_reject_unknown_tool_names`) and `_advertised_names`
  (`:247-250`) and both intersect — but `harness_autonomy` is a third dial in the same frozen model
  that moves in the other direction, and neither docstring carves it out.
- **Evidence**: the `rogue` row above (`gate_applies=False` under a `plan_only` global) is the
  widening executed. The inverse direction is the only one under test: `tests/test_plan_gate.py:415-420`
  sets the global to `execute` and asserts a `plan_only` profile *gains* the gate. No test asserts
  what a profile can take away.
- **Fix**: decide the direction and enforce it. Either resolve autonomy as a monotone narrowing —
  `plan_only` if either the global or the profile says `plan_only` — which makes the docstrings true
  and costs one `or`; or keep the override and correct both docstrings to say that autonomy is an
  operator-authored dimension that a file can widen, and add it to whatever reviews profile files.
  Silence is the worst of the three.

## 3. `resolve_precondition`'s docstring claims build-time resolution; the only caller resolves it at launch

- **Severity**: medium
- **Location**: `src/chemclaw/connectors/jobs.py:124` (the claim) vs `:307` (the sole call site,
  inside `prepare_job_launch`)
- **Trigger**: a `connector.yaml` whose `jobs[].precondition` names an attribute that does not exist —
  e.g. `chemclaw.science.bo.problem:require_campaign_startble` (one letter dropped).
- **Consequence**: the docstring says "Resolved at build time (and by `make connector-validate`), not
  at call time, so a typo is a configuration error a deployment finds before a chemist does." The
  process builds cleanly, advertises the job tool to the model, and the reference is only touched on
  the launch path — so in a deployment that skipped or predates the validator, the failure lands
  exactly where the comment promises it will not: on the chemist, mid-conversation, as a
  `ConnectorJobError`. Worse in kind than a typo: a job's *domain guard* is the thing that is
  missing, and its absence is invisible until someone tries to run the job.
- **Evidence**: `grep -rn resolve_precondition src/` returns exactly two callers —
  `cli/validate_connectors.py:194` and `jobs.py:307`. Nothing on the build path. Reproduced with a
  copied bundle at `/tmp/conn2/bo` carrying the misspelled reference, ahead of the shipped dir:
  ```
  CHEMCLAW_CONNECTORS_DIR=/tmp/conn2:src/chemclaw/connectors
  BUILD OK, advertised: True
  params model: CampaignSpec ['problem','objective_name','n_initial','n_rounds','batch','seed']
  # and the reference itself:
  ConnectorJobError precondition '...:require_campaign_startble':
      'chemclaw.science.bo.problem' has no 'require_campaign_startble'
  ```
  `make connector-validate` does check it and is in `.github/workflows/ci.yml:122`, so CI catches it
  — but that is the *validator*, not "build time", and the two are not interchangeable for a
  deployment that mounts a private bundle dir (a seam `core/config/connectors.py:30` explicitly
  advertises) which CI never saw.
- **Fix**: resolve it in `_build_params_model`'s neighbourhood — `build_job_tool` already has the
  `JobSpec` — and cache it beside `_PARAMS_MODELS`, so a bad reference fails the process rather than
  the chemist. Then the docstring is true. If launch-time resolution is deliberate (it is the only
  replay-safe place to *call* it, which is a different question from where to *resolve* it), say so
  and drop the "build time" clause.

## 4. `templates_dir` and `profiles_dir` are CWD-relative while every sibling seam is package-anchored — both silently resolve to nothing outside the repo root

- **Severity**: medium
- **Location**: `src/chemclaw/core/config/agent.py:194` (`profiles_dir: str = "data/profiles"`) and
  `:200` (`templates_dir: str = "data/templates"`), against `core/config/shipped.py:_shipped` used by
  `connectors_dir` and `data_sources_dir`
- **Trigger**: start any Chemclaw process from a working directory other than the repo root (or from
  an installed wheel with no `data/` beside the CWD): `cd /tmp && uv run --project <repo> chemclaw chat`,
  or `oc exec` into a pod and run the `chemclaw` console script from `$HOME`.
- **Consequence**: `templates.registry.discovered()` returns `{}`, so every `run_<name>` template tool
  disappears from the advertised surface — silently, because an empty `templates_enabled` means "all
  discovered", and zero discovered is a legal answer. `profile_discovery.profile_files()` returns
  `[]`, so every narrowed profile is unregistered and `POST /sessions {"profile": "safety"}` 400s
  (loud) while a session that *needs* the narrowing simply cannot be created. The container hides
  this: `deploy/Containerfile` sets `WORKDIR /app` and `COPY data ./data`, so the default path works
  there and nowhere else. `_shipped` exists precisely because CWD-relative declaration paths "only
  resolved when the process happened to be started from the repository root"; two of the four roots
  were left behind.
- **Evidence**: run from `/tmp`:
  ```
  templates_dir= ['data/templates']   profiles_dir= ['data/profiles']
  connectors_dir= ['/home/user/Chemclaw3/src/chemclaw/connectors']
  templates: []
  profile files: []
  connectors: ['bo','calc','chem','molfp','qm','rxnfp','safety']
  ```
  Same process, same config: two seams found their declarations, two found nothing and said nothing.
- **Fix**: move `data/` (or at least `data/templates` and `data/profiles`) inside the installed
  package and default both fields through `_shipped(...)`, exactly as the connector and source roots
  do. If `data/` must stay outside the package, then at minimum log a WARNING when a configured
  templates/profiles directory does not exist — the registries currently `continue` past a missing
  root without a word (`templates/registry.py:86`, `profile_discovery.py:80`).

## 5. Two directory-discovery seams, two opposite duplicate-name policies — and the fatal one takes the whole agent down

- **Severity**: low
- **Location**: `src/chemclaw/templates/registry.py:90` (raise) and `agent/profile_discovery.py:100`
  (raise) vs `connectors/registry.py:124` and `ingest/sources/registry.py:73` (`found.setdefault`,
  first-dir-wins, silent)
- **Trigger**: put a second directory ahead of the shipped one, holding a same-named declaration —
  the documented override mechanism for connectors ("First dir wins, so an operator's private
  connectors dir listed ahead of the repo's can override a shipped bundle — the same precedence a
  `PATH` entry has", `connectors/registry.py:120`). Do the same thing to templates.
- **Consequence**: for connectors and data sources the private copy silently wins; for templates and
  profiles the process refuses to start. And the templates refusal is not confined to templates:
  `discovered()` is reached from `_register_generated_tools` → `_capability_tools`, so it fails
  *every agent build in the process*, i.e. the front door answers no turns at all because two files
  in two directories share a stem. An operator applying the `PATH`-style idiom that one seam
  documents gets a total outage from its sibling.
- **Evidence**:
  ```
  CHEMCLAW_TEMPLATES_DIR=/tmp/tpl:data/templates
    discovered() -> RAISED TemplateError data/templates/hazard-briefing.yaml:
                    template 'hazard-briefing' is already defined
    advertised_tool_names() -> AGENT BUILD FAILS: TemplateError (same message)

  CHEMCLAW_CONNECTORS_DIR=/tmp/conn:src/chemclaw/connectors
    discovered()['safety'] -> /tmp/conn/safety      # silent override, no warning
  ```
- **Fix**: pick one rule for all four seams. `setdefault` + an INFO line naming the shadowed path is
  the one that matches the documented `PATH` idiom and cannot take a deployment down; a *within-one-directory*
  duplicate should still raise, since that one is unambiguously an authoring mistake.

## 6. Stale comment: "an undeclared tool is treated as a read" — the validator refuses to load instead

- **Severity**: low
- **Location**: `src/chemclaw/connectors/manifest.py:233`, contradicted by `_check_classification`
  at `:188-218` in the same file
- **Trigger**: reading the file. A manifest whose endpoint serves a tool listed in neither
  `state_changing` nor `read_only`.
- **Consequence**: the block comment above `Endpoint` states the fail-open default ("An undeclared
  tool is treated as a read: core cannot infer a bundle's semantics, and guessing 'write' would gate
  every connector's whole surface the day this shipped"), while `_check_classification` raises
  `endpoint does not say whether tool(s) [...] change state` and its own docstring argues the exact
  opposite ("Refusing to load is the only option that cannot be wrong quietly"). Two paragraphs, 40
  lines apart, describing incompatible behaviours for the input that decides whether the plan gate
  refuses a tool. A reader who trusts the first one concludes that omission is safe.
- **Evidence**: both texts are in `manifest.py`; the partition check is unconditional in
  `HttpEndpoint._every_tool_is_classified` and `StdioEndpoint`'s twin, so the code implements the
  docstring and not the comment.
- **Fix**: delete the last sentence of the `Endpoint` comment (from "An undeclared tool is treated as
  a read") — the behaviour it describes was replaced by the partition, and the docstring below already
  carries the argument.

## 7. The manifest tree that drives `importlib` is writable by the process that reads it, and the reason given for allowing that no longer holds

- **Severity**: medium
- **Location**: `deploy/Containerfile:104-105` (`chown -R 1001:0 /app && chmod -R g=u /app`),
  `deploy/helm/chemclaw/values.yaml:627-631` (`readOnlyRootFilesystem: false`)
- **Trigger**: any write primitive available to the running process — a path-traversal in a file
  write, a dependency's arbitrary-write bug, `oc exec` by anyone holding pod/exec. Write
  `precondition: os:system` (or an `ingest:`/`driver:` reference, or a whole new bundle directory)
  into `/app/src/chemclaw/connectors/<x>/connector.yaml`; the next process start imports the named
  module.
- **Consequence**: the declaration set is not read-only, so a transient write becomes persistent code
  execution across restarts and across every replica sharing the image layer's mutated copy. This is
  the difference between "manifests are trusted because they ship in the image" (the assumption the
  whole seam rests on) and "manifests are trusted because they are *immutable* in the image", and
  only the second is a control.
- **Evidence**: `values.yaml:628-630` justifies the disabled read-only root filesystem as "the
  calculation workers shell out to xtb/crest, which need writable scratch". That is no longer true of
  this repository: the only `subprocess`/`create_subprocess_exec` in `src/` are `kg/git_submitter.py:201`
  (git) and `cli/live_storm.py:233`; `connectors/calc/server/app.py:10` states in the present tense
  "There is no binary to ask any more", and `science/calc/thermo.py:14` says the RRHO arithmetic
  "need[s] no binary, no tblite and no crest". The physics moved to `Chemclaw3-mcp`. The comment's
  own exit condition ("Turn it on once the deployment mounts an emptyDir over the process's temp
  directory") is therefore already met for everything except the git clone.
- **Fix**: two independent changes, both cheap. (a) In the Containerfile, restrict the writable set:
  `chown -R 1001:0 /app` only where the process must write, and leave `src/`, `data/`, `skills/` and
  `knowledge/` owned by root and group-readable — the runtime user never writes a manifest. (b) Set
  `readOnlyRootFilesystem: true` with an `emptyDir` at the temp dir and at the knowledge clone path,
  and re-verify the claim in the comment before writing a new one.

---

## Things I checked that turned out fine

Recording these so the negative result is auditable rather than an omission:

- No `eval`/`exec`/`__import__` on any config-derived string anywhere in `src/`.
- `connectors/registry.py:218 server_tools_module` is reached only by the two validators and two
  tests — no serving path imports a bundle's server package into the front door, so a bundle's
  server module is not a runtime import surface at all.
- `ingest/sources/registry.py:113 resolve_half` and `warehouse/connect.py:38 _resolve_driver` both
  check `callable`, both fail with the module and attribute named, and both are deliberately
  uncached; the "a process that never queries the warehouse never imports the vendor client"
  property in the `connect.py` docstring is real (the resolve happens inside `open_warehouse`, not at
  module scope).
- `warehouse/connect.py:connect_options` calls `register_secret_env(variable)` *before* reading each
  credential, as its docstring claims, and treats an empty string as absent.
- `templates` never imports anything — a template step names a tool or job by string and is resolved
  against the *built* surface (`durable/template_activities.py:_invoke`), through `invoke_governed`,
  under the requester's restored identity. `step_profile` intersects rather than unions
  (`template_activities.py:482`), so a template cannot hand a step a write its profile never
  advertised.
- `AgentProfile.tool_names` / `mcp_server_names` narrow both halves of the surface and reject unknown
  names loudly (`chemclaw_agent.py:322`), as documented.
