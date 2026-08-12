# MAF removal audit — an independent re-audit (2026-08-12)

**Scope.** Phase 1 of a full review/refactor/hardening sweep. Asks one question: *is the Microsoft
Agent Framework actually gone, on every surface?*

**Method.** Deliberately independent. The audit lane was run without reading
`D-2026-08-11-what-the-removal-found` or `D-2026-08-12-a-review-the-migration-did-not-get`, so its
findings are not downstream of theirs; the diff against both is §6 and is the most interesting part
of this document. Ground truth for what MAF actually *was* here came from `git show` of the removal
commits (`e453c20`, `b03209b`, `4054cdd^`), not from guessing package names.

**Every claim below is a count of definitions against references, not a reading of prose.**

---

## 1. Ground truth: what MAF was in this repo

The brief's candidate names were mostly wrong, which matters — an audit that greps for the wrong
string reports "clean" and is believed.

| | Actual |
|---|---|
| Distributions | `agent-framework-core>=1.11.0`, `agent-framework-anthropic`, `agent-framework-openai` |
| Import root | `agent_framework` (+ `agent_framework_anthropic`, `agent_framework_openai`, private `._compaction`, `._tools`, `._harness._loop`) |
| Symbols used | `Content`, `Message`, `tool as as_tool`, `FunctionInvocationLayer`, `UsageDetails`, `AgentMiddleware`, `SkillsProvider`/`SkillsSource`, `OpenAIChatClient`, `AgentSession`, `HistoryProvider`, `build_agent` |

**Decoys, confirmed against the pre-removal tree: `microsoft.agentframework`, `AgentThread`,
`ChatAgent`, `ChatMessageStore`, `MCPStreamableHTTPTool`, `AzureAIAgentClient`, `AnthropicChatClient`
had 0 hits even *before* removal.** They were never used here, so their absence today is evidence of
nothing.

---

## 2. `MAF-REMOVED` — the mechanical removal is complete, and it is good work

| Surface | Result |
|---|---|
| Source imports | `agent_framework` in **0** files under `src/`, `tests/`, `examples/` |
| `pyproject.toml` / `uv.lock` | Clean. **Programmatic orphan check over all 240 locked packages: 0** are neither a direct dependency nor required by another. `opentelemetry-api` correctly re-justified (`pyproject.toml:54-59`) as an `opentelemetry-sdk` requirement, not a MAF transitive |
| Config | No MAF key, flag or env var in `core/config/*` or `.env.example`. All **349** `CHEMCLAW_*` keys resolve to a config attribute |
| CI / build | `.github/workflows/ci.yml`, `image.yml`, `Makefile`, `deploy/Containerfile`, `deploy/`, `infra/` clean |
| Layering ratchet | `tests/test_third_party_layering.py:94-110` — no `maf` key in `_STACKS`, no allow-list rows |
| Prior rename pass | `lg_`-prefixed middlewares: **0** hits. `make_langgraph_audit_middleware`: gone |
| Top-level docs | `CLAUDE.md:75`, `README.md:3`, `ARCHITECTURE.md`, `deploy/README.md:260-272` — correctly past-tense |

### Checked and cleared — recorded so a later sweep does not re-find them

- `infra/sql/043_session_message_shape.sql:33` `DEFAULT 'maf'` is a **live data value** with real
  readers (`agent/message_migration.py:56,240`), not a remnant.
- `agent/compaction.py:215` uses **LangChain's** `AgentMiddleware`, not MAF's (stated at `:219`).
- `agent/message_migration.py` looks like a migration shim but is load-bearing for legacy rows
  (`agent/session_store.py:51-54,87`).
- `mode_set`, `grant_execute`, `ToolApprovalMiddleware`, `harness_mode`, `_NoChatClient`,
  `UsageDetails`, `FunctionInvocationLayer`: **0 definitions**, only past-tense prose.
- `docs/reference/architektur.md`, `docs/planning/implementation-plan.md`, `implementation-tickets.md`
  each open by declaring themselves historical — their MAF mentions are correct.

**Verdict: the dependency surface, lockfile, CI, config and ratchets are genuinely clean.** What
survived is entirely prose and test doubles.

---

## 3. `MAF-REMNANT` — dead symbols cited as live

### R1 — `build_agent`: **42 live references to a function with 0 definitions** ⚠ largest

```
$ grep -rn "def build_agent\b" src/ tests/ | wc -l        →  0
$ grep -rn "\bbuild_agent\b" src/ tests/ | grep -v build_langgraph_agent | wc -l  →  42
```

The replacement is `build_langgraph_agent` (`agent/langgraph_agent.py:97`).

Sites: `agent/profiles.py:5,7,13,18,36,40` · `agent/skill_access.py:8,34` · `agent/skill_manifest.py:100` ·
`agent/chemclaw_agent.py:310,454` · `agent/langgraph_agent.py:5,111,119,128,135` · `api/runner.py:161,188` ·
`cli/chat.py:5` · `cli/validate_skills.py:41,198` · `connectors/registry.py:21` ·
`connectors/calc/specs.py:5` · `connectors/qm/specs.py:5`, `cache.py:12`, `knowledge.py:27` ·
`evals/live.py:9` · `core/config/llm.py:60` · `core/identity_context.py:11` ·
`core/tool_registry.py:8,14` · `core/logging.py:876` · plus 9 test modules.

### R2 — `*SkillsSource`: **16 references to classes with 0 definitions**

```
$ grep -rn "class .*SkillsSource" src/ tests/ | wc -l  →  0
$ grep -rn "SkillsSource" src/ tests/ | wc -l          →  16
```

Real names: `EnabledSkills` (`agent/skill_access.py:78`), `ToolScopedSkills` (`:108`),
`RoleScopedSkills` (`:151`). `FileSkillsSource` has **no successor at all** — it was replaced by
`settings.skills_dirs` + `connectors.registry.skills_dirs` (`agent/langgraph_agent.py:367`).

**The damaging instances are operator-facing.** `make skill-validate` prints the dotted path
`chemclaw.agent.skill_access.ToolScopedSkillsSource` (`cli/validate_skills.py:17,136`; also
`:27,163,178` for `RoleScopedSkillsSource` / `EnabledSkillsSource`). An operator who follows that
path gets an `ImportError`. This is the only finding in this document that is **broken output rather
than stale text**, and it should be fixed first.

Other sites: `agent/langgraph_agent.py:198,380` · `agent/skill_manifest.py:27,84,105` ·
`connectors/registry.py:6,216` · `core/config/agent.py:181`.

### R3 — `core/session_context.py:18-20` — a module split explained by a dependency that is gone

> "The other half of this ambient — the live `AgentSession` **object** — stayed behind in
> `agent/session.py`, **because it needs `agent_framework`** and so cannot be kernel material."

`agent/session.py` imports only `dataclasses` and `typing` (`:25-26`) and defines `TurnSession`, not
`AgentSession`. The stated reason for the split is now false; the split itself may still be right,
but nothing in the tree says why.

### R4 — `core/logging.py:143` — the same defect D-2026-08-11 §3 fixed, one file away

> "**MAF creates** one duration histogram per exposed MCP function, and this system rebuilds its
> connector tool surface every turn — so a turn with telemetry *off* leaked 35 `_ProxyMeter`s,
> 35 `_ProxyHistogram`s, 70 locks and 35 lists, permanently."

Nothing in `src/` calls `get_meter`/`create_histogram` — **the instrument-creating party was MAF**.
The `_install_noop_meter_provider` guard remains defensible as defence-in-depth, but its
justification and its measurements no longer describe this tree. `core/metrics.py:326-334` had this
identical sentence rewritten correctly by D-2026-08-11 §3; the sweep missed the neighbouring module
in the same package. Propagates to `connectors/server_entry.py:20`, `tests/test_connector_transport.py:400`.

### R5 — `tests/test_service.py:45-50` — a double documented against a deleted function

> "`is_connected` is what **`open_reachable` reads**… and **MAF reads it too**"

`connectors.registry.open_reachable` was deleted in `e453c20`. The live readers are
`core/metrics.py:109` and `api/runner.py:627`. Both halves of the sentence are false.

### R6 — `tests/fakes.py:14-21,39-63` — a shared double modelling an interface with no production reader

`FakeUpdate.user_input_requests` derives from `content.user_input_request`, which has **0 readers in
`src/`** (only `tests/fakes.py`, `tests/fakes_langgraph.py:7`). `src`'s `approval_request` is an
unrelated mechanism (`core/turn_signals.py:208`, `api/events.py:179`). The module docstring's
justification — "the runner's approval branch was executed by no test in the suite" — describes a
branch that no longer exists. (`update.contents` *is* still live: `api/runner_trace.py:134`.)

### R7 — `examples/research_demo.py:3` — present tense in a user-facing example

> "The MAF agent needs a provider API key to actually converse"

---

## 4. `MAF-PARTIAL`

| # | Location | Issue |
|---|---|---|
| P1 | `cli/mock_llm.py:471` | `/v1/responses`, the MAF-era Responses API route. Production builds `ChatOpenAI` → `/v1/chat/completions` (`agent/llm_provider.py:83-86`). Kept alive only by `tests/test_live_storm.py:396`. **A knowing shim**, argued at `:395` — flagged for visibility, not as oversight |
| P2 | `api/routes/turns.py:82` | "the provider **stores** an opaque MAF payload" — present tense; the provider now writes LangChain `message_to_dict` payloads, and only legacy rows are MAF-shaped |
| P3 | `tests/test_cli.py:122` | **A rename pass edited text inside a quoted error string:** `"requires an TurnSession"`. The real historical error was `requires an AgentSession` (cf. `durable/template_activities.py:353`, `cli/chat.py:128`). Now both ungrammatical and false as a historical record |
| P4 | `docs/reference/architektur.md:262` | Inside an explicit **"Stand heute (D-052, D-2026-08-01)"** current-state annotation: "Rollenbewusstes Skill-Filtering **existiert als** `RoleScopedSkillsSource`". The historical-document carve-out does not cover a block declaring itself current |
| P5 | `docs/guides/xtb-tools-proposal.md:178` | "nothing in `build_agent` is edited" — present tense in a forward-looking proposal, not a historical doc |
| P6 | `tests/test_review_2026_08_05.py:366,380`; `tests/test_live_storm.py:360` | Low severity: "MAF's shapes vary by version", "MAF routinely does", "MAF re-invokes the model" |

---

## 5. The bug class the brief asked for: a setting with no reader

Method: extract every field from `core/config/*.py`, grep each repo-wide across `py/yaml/toml/sh`,
then **manually clear indirect readers** — a naive `settings.<field>` grep produced 22 candidates of
which ~19 were false positives, because computed properties wrap raw fields.

**Genuine finds (not MAF-related, but the class asked for):**

- `calibration_conformal_coverage` — `core/config/calculators.py:193`
- `calibration_conformal_min_samples` — `core/config/calculators.py:194`

Zero readers anywhere: no `src/`, no `tests/`, no chart. Both are documented and shipped in
`.env.example:234-235`. `science/calc/uncertainty.py:195` `conformal_uncertainty(coverage=…, …)`
takes exactly these two parameters and is **never wired to the settings**. Operator-visible config
that governs nothing. *Found independently by two methods in this sweep, which is why it is listed
as confirmed rather than candidate.*

**Cleared as false positives** (indirect reader confirmed): `entra_issuer`/`entra_jwks_url`/
`entra_tenant_id` → derived `entra_issuer_url`, `api/auth.py:160` · `templates_dir` →
`templates_dirs`, `templates/registry.py:84` · `service_max_connections`/`service_keepalive_seconds`/
`service_max_header_bytes` → uvicorn flags via Helm, pinned `tests/test_deploy_chart.py:1089-1094` ·
`pg_fleet_pooled_processes`/`service_fleet_replicas` → derived-property inputs, pinned
`tests/test_config.py:489-638` · `skills_dir` → `settings.skills_dirs`.

**Still open, weaker:** `service_uvicorn_workers` has no code reader — only prose
(`agent/session_store.py:391`, `infra/sql/018_session_turns.sql:8`) — and the Helm chart states
there is "no uvicorn-worker factor" (`_helpers.tpl:489`). A knob describing a deployment dimension
the deployment does not use.

---

## 6. The diff against the two prior reviews — why these survived

This is the part that justified running the audit independently.

**`D-2026-08-11-what-the-removal-found` §4 explicitly claimed this ground.** It reports sweeping
"~180 `MAF` mentions in `src/`", keeping the load-bearing history and rewriting the present-tense
assertions, and it even names "a module docstring describing `build_agent` and a `SkillsProvider`"
as one of the things it rewrote. The rule it set is right: *past tense about the framework is
evidence; present tense about it is false.*

**The sweep was keyed on the token `MAF`. Measured:**

```
$ grep -rn "\bbuild_agent\b" src/ tests/ | grep -v build_langgraph_agent | grep -ci maf   →  0
```

**Not one of the 42 surviving `build_agent` references mentions MAF.** They name a function that no
longer exists, without naming the framework it belonged to — so a MAF-keyed sweep was structurally
incapable of seeing them, however carefully it was run. The same holds for the 16 `*SkillsSource`
references and for R3/R5/R6, each of which describes a dead symbol rather than a dead framework.

**`D-2026-08-12-a-review-the-migration-did-not-get`** ran 16 lanes over the 242-file diff with two
refute-by-default verifiers per finding, and did not catch these either — for a different and more
interesting reason. It was **diff-scoped**: it asked what the 242 changed files got wrong. R1, R2,
R4 and R5 live in files the migration *did not touch*; they became false when a symbol elsewhere was
renamed. A diff review cannot see a file that did not change, and a token-keyed sweep cannot see a
symbol that does not carry the token. **Both methods were sound and their union still has this
hole.**

The generalisation, which is the actionable part:

> A rename makes prose false in files the rename never opened. Neither "grep the old framework's
> name" nor "review the diff" can find that. The only check that can is **"does every symbol this
> tree names in the present tense still exist?"** — which is mechanisable, and currently unmechanised.

`make prose-validate` already resolves metric names and tool names against the live registries. The
same idea applied to code identifiers in docstrings would have caught all of R1, R2 and R5 at CI
time. That is recorded as a backlog row rather than built here, because it is a new gate and this
sweep is behaviour-preserving.

---

## 7. Verdict

| Class | Count | Severity |
|---|---|---|
| `MAF-REMOVED` | every dependency, config, CI and ratchet surface | — |
| `MAF-REMNANT` | 7 findings, ~58 sites | R2 is operator-visible breakage; R1 is scale; R3–R7 are stale premises |
| `MAF-PARTIAL` | 6 findings | P3 is a corrupted historical record; P1 is a knowing shim |
| Setting-with-no-reader | 2 confirmed + 1 weak | not MAF-caused |

**The framework is gone. Its vocabulary is not.** Nothing here changes runtime behaviour except R2,
which prints an unimportable path to an operator running a validator. The fix for R1/R2 is two
mechanical rename passes; the fix for R3–R7 is deleting or correcting six paragraphs and two test
doubles. The durable fix is the identifier-resolution gate in §6.
