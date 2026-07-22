# Phase 1 — Inventory & Provenance Mapping

**Repository:** `/home/user/Chemclaw3` · **Audit date:** 2026-07-22 · **Method:** static read-only inspection (no files modified)
**Current branch:** `claude/codebase-audit-hardening-69v233` (off `main`) · **History depth:** 97 commits, 9 merges

---

## 1. Directory / Module Tree

Sixteen top-level Python packages plus infra/deploy. One-line purpose per package (from package `__init__.py` docstrings, verified against code):

| Module | Purpose | Clarity |
|---|---|---|
| `agents/` | MAF conversation layer: the agent (`chemclaw_agent`), its tools, identity/audit/authz, LLM provider seam, session + CLI. | clear |
| `bo/` | Bayesian optimization layer over BoFire, kept behind neutral `problem`/`objectives` types (`engine` is the only BoFire importer). | clear |
| `calc/` | Compute layer: calculation result store (compute-once cache) + fast calculators (xTB/GFN2, pKa, solubility). | clear |
| `chemclaw/` | Shared kernel: the single typed config, DB helper, logging, ids, errors, chem helpers, temporal client. | clear |
| `eln/` | ELN ingestion: canonical ORD schema + adapters (JSON free-text, native ORD) behind the `ElnAdapter` contract + sync/validate/cursor. | clear |
| `evals/` | Evaluation & metric layer: pure-function metrics + registry (`metric`), harness, tool-utility A/B (`ab`). | clear |
| `kg/` | Knowledge graph: Markdown note schema, NetworkX indexer, validation, PR-gate, git submitter, render. | clear |
| `mcp_servers/` | MCP capability servers: molecule (ECFP4) and reaction (DRFP) fingerprint search over a shared generic `fpstore`. | clear |
| `memory/` | Agent memory layers: episodic campaign/optimization chains + semantic playbook distillation (no new infra). | clear |
| `report/` | On-demand report / deep-research harness over internal notes (`harness`, `evidence` contract, `retrievers`). | clear |
| `service/` | ASGI front door (FastAPI + SSE): the actual runner of the agent for a chemist (`app`, `runner`, `events`, `auth`). | clear |
| `sources/` | Generic `DataSource` seam (F7): composes the ingest (ELN) + retrieve (graph) halves + config registry. | clear |
| `workers/` | Two Temporal worker processes: `hpc_worker` (hpc-jobs queue) and `background_worker` (background-jobs queue). | clear |
| `workflows/` | Temporal durable-execution layer: workflow definitions + their activities (QM, BO, ELN sync, memory, report, publish). | clear |
| `scripts/` | Operational CLIs: `schedules` (Temporal schedules), `validate_ord`, `validate_skills`. | clear |
| `infra/` | docker-compose dev stack + `sql/` migrations. | clear |
| `deploy/` | OpenShift delivery: Containerfile, entrypoint, Helm chart (F6). | clear |

No module's purpose is genuinely ambiguous — the docstring discipline required by `CLAUDE.md` is consistently applied. Two naming subtleties that could confuse a reader (both intentional, documented in-file):
- `evals/metric.py` (singular = interface + `@metric` registry) vs `evals/metrics.py` (plural = concrete scored functions).
- `calc/store.py` (store interface + `cached_compute`) vs `calc/postgres_store.py` (the Postgres backend of that interface).

---

## 2. Entry Points

### CLI / console scripts
- **`pyproject [project.scripts]`:** `chemclaw = "agents.cli:main"` — the only console script. Terminal agent front door; runs **only** with `--admin` (bypasses Entra, stamps `settings.cli_admin_actor`). `agents/cli.py` `main()`.
- **`python -m` module entrypoints** (each has `if __name__ == "__main__"` / `main()`), wired through the `Makefile`:
  - `calc.migrate` — DB migrations (`make db-migrate`)
  - `scripts.schedules` — apply Temporal Schedules (`make schedules-apply`)
  - `kg.validate` — knowledge-graph validation (`make kg-validate`)
  - `evals.harness` — score eval case-set (`make eval`)
  - `eln.validate` — ELN reaction validation (`make eln-validate`)
  - `scripts.validate_skills` — SKILL.md frontmatter check (`make skill-validate`)
  - `workers.hpc_worker`, `workers.background_worker` — Temporal workers
  - `mcp_servers.molfp.server`, `mcp_servers.rxnfp.server` — stdio MCP servers (spawned by the agent per `settings.mcp_servers`)

### HTTP routes (`service/app.py`, FastAPI via `create_app`)
| Method | Path | Handler | Auth |
|---|---|---|---|
| GET | `/healthz` | `healthz` — liveness | none |
| GET | `/readyz` | `readyz` — readiness (agent builds) | none |
| POST | `/sessions` | `create_session` | `require_principal` |
| POST | `/sessions/{session_id}/messages` | `post_message` — run one turn, SSE stream | `require_principal` + ownership check |
| GET | `/sessions/{session_id}/events` | `session_events` — job→session push-back SSE | `require_principal` + ownership |
| GET (mount) | `/` | static chat UI (`service/static`, if present) | none |

Auth dependency: `service/auth.py:require_principal` — Entra OIDC JWT validation (signature/audience/issuer/exp), dev stand-in when `entra_required=False`.

### Scheduled jobs (`scripts/schedules.py` → Temporal Schedules, **not** host cron)
`planned_schedules()` defines four, all on `background-jobs` queue:
- `eln-sync` → `ElnSyncWorkflow` (interval `eln_sync_schedule_minutes`, default 60m)
- `campaign-synthesis` → `CampaignSynthesisWorkflow`
- `playbook-distillation` → `PlaybookDistillationWorkflow`
- `optimization-campaign` → `OptimizationCampaignWorkflow` (last three interval `memory_synthesis_schedule_minutes`, default 1440m)

### Workers / consumers
- `workers/hpc_worker.py` — hosts `QMJobWorkflow` + QM activities (`prepare_input, submit_to_hpc, poll_hpc_status, parse_qm_output`) on `hpc-jobs`.
- `workers/background_worker.py` — hosts BO campaign, ELN sync, interaction-approval, knowledge, memory, report workflows + activities on `background-jobs`.

### DB migrations (`infra/sql/`, applied by `calc/migrate.py`)
`000_schema_migrations` (ledger), `001_calculation_results`, `002_molecule_fingerprints`, `003_reaction_fingerprints`, `004_fingerprint_definition`, `006_audit_events`, `007_sync_cursors`, `008_sessions`, `009_session_events`.
**⚠ Flag:** the sequence skips `005` (gap between `004` and `006`) — no `005_*.sql` file exists. Likely a dropped/renumbered migration during the branch merges; worth confirming nothing was lost.

---

## 3. External Dependencies (`pyproject.toml`)

Runtime pins (all lower-bound `>=`, no upper caps):

| Package | Pin | Role |
|---|---|---|
| `agent-framework-core` | `>=1.11.0` | MAF orchestration |
| `agent-framework-anthropic` | `>=1.0.0b260709` | MAF Anthropic chat client (dev path) |
| `agent-framework-openai` | `>=1.0.0b260709` | MAF OpenAI-compatible client (prod path) |
| `bofire[optimization,cheminfo]` | `>=0.4.1` | BO engine (pulls mordredcommunity/BoTorch) |
| `drfp` | `>=0.3.7` | reaction fingerprints |
| `rdkit` | `>=2026.3.4` | cheminformatics |
| `tblite` | `>=0.7.0` | GFN2-xTB energies |
| `scikit-learn` | `>=1.9.0` | solubility model |
| `numpy` `>=2.0`, `pandas` `>=2.2` | | numerics |
| `temporalio[pydantic]` | `>=1.30.0` | durable execution |
| `fastapi` `>=0.139.2`, `uvicorn` `>=0.51.0`, `sse-starlette` `>=3.4.5` | | front door |
| `mcp` | `>=1.2.0` | capability protocol |
| `networkx` | `>=3.6.1` | KG indexer |
| `psycopg[binary]` | `>=3.2` | Postgres |
| `pydantic-settings` | `>=2.5` | config |
| `pyjwt[crypto]` | `>=2.8` | Entra JWT validation |
| `python-frontmatter` `>=1.3.0`, `pyyaml` `>=6.0` | | note frontmatter |

Dev group: `ruff>=0.6`, `mypy>=1.11`, `pytest>=8.3`, `pytest-cov>=5.0`, `pre-commit>=3.8`, `types-pyyaml`.

### Duplicated-functionality flags
- **HTTP client — one library, but undeclared:** `httpx` is imported directly in `agents/identity/obo.py`, `agents/identity/workload.py`, `agents/llm_provider.py`, and `workflows/hpc/nextflow.py`, yet **`httpx` is not listed in `pyproject.toml` dependencies** — it is relied on transitively (pulled by `fastapi`/`mcp`/agent-framework). This is a genuine risk: a transitive dep drop would break four modules. `chemclaw/db.py` uses stdlib `urllib.parse` only (DSN parsing, not an HTTP client), so there is **no** competing HTTP stack — just an undeclared-direct-dependency issue.
- **Config — single mechanism, clean:** exactly one config surface (`chemclaw/config.py`, pydantic-settings). No competing `os.environ` reads for app config (see §4). No duplication.
- **Calculation/fingerprint stores — intentional shared abstraction, not duplication:** `calc/store.py` (interface) + `calc/postgres_store.py` (backend) and `mcp_servers/fpstore.py` (generic Tanimoto store serving both molfp & rxnfp) are explicit Rule-of-Three extractions, documented as such. Not a red flag.
- **Two registries with the same pattern:** `eln/registry.py` (`ELN_ADAPTERS`) and `sources/registry.py` (`DATA_SOURCES`) both map name→factory. `sources/registry.py` re-hosts the ELN adapters verbatim and is documented as the generalization (F7) of the ELN one. The ELN registry still exists and is still read by the sync/memory paths — so there are **two live registries covering overlapping ground**. Worth watching for eventual consolidation, but currently deliberate (F7 seam layered over the pre-existing ELN registry without deleting it).

---

## 4. Config Surfaces

**Single source of truth:** `chemclaw/config.py` — one `Settings(BaseSettings)` singleton, `env_prefix="CHEMCLAW_"`, `env_file=".env"`, `extra="forbid"`. ~90 fields, all documented, all with defaults targeting the local compose stack. Six `@model_validator` fail-fast checks (llm provider completeness, Entra enforcement completeness, Temporal mTLS cert/key pairing, poll-vs-heartbeat, knowledge_dir relativity).

**`os.environ` usage is deliberately minimal (grep-verified) — no shadow config:**
- `agents/llm_provider.py` — reads `ANTHROPIC_API_KEY` (the provider client's own credential, by design not stored in config).
- `chemclaw/logging.py` — sets `OTEL_EXPORTER_OTLP_ENDPOINT` from `settings.otel_endpoint` (config→env bridge for MAF's OTel).
- `tests/test_config.py` — test isolation only.

**Config files:** `.env` (git-ignored; `.env.example` documents 115 `CHEMCLAW_*` vars), `infra/docker-compose.yml` (wired to same var names), `deploy/helm/chemclaw/values.yaml` + `config.yaml` template.

**Secrets** (values empty by default; supplied via env / mounted secrets / Helm three-secret model): `CHEMCLAW_LLM_API_KEY`, `CHEMCLAW_HPC_API_TOKEN`, `CHEMCLAW_TEMPORAL_TLS_CERT/KEY/CA`, `CHEMCLAW_TEMPORAL_API_KEY`, `CHEMCLAW_POSTGRES_DSN`, `CHEMCLAW_SESSION_STORE_DSN`, `CHEMCLAW_LLM_TLS_CA_BUNDLE`, plus workload-federation token path `CHEMCLAW_ENTRA_SA_TOKEN_PATH`. No secrets manager client in-code — secrets arrive as env/mounted files (documented as OpenShift secrets in F6).

**Feature flags (all in config, default-off "safe fallback" posture):**
- `harness_enabled` (classic vs autonomous plan/execute agent), `harness_autonomy` (`plan_only`/`execute`)
- `entra_required` (auth enforcement), `entra_workload_federation_enabled`, `entra_obo_enabled` (dormant)
- `otel_enabled`, `otel_include_sensitive_data`
- `hpc_launch_interface` (`mock`/`nextflow`), `session_store` (`memory`/`postgres`), `llm_provider` (`anthropic`/`openai_compatible`)
- `data_sources` / `eln_sync_adapter` (which sources active), `skill_role_gates` (RBAC)

---

## 5. Test Coverage (approximate, per module)

87 source `.py` modules; **68 test files** in `tests/`, all active — **zero `pytest.mark.skip`/`xfail`/`pytestmark` markers** found in test bodies (the 25 runtime skips are environment guards for Postgres/Temporal, see `02-baseline.md`). `tests/conftest.py` provides a `fast_mock` autouse fixture (shrinks mock-HPC sleeps) and a shared `FakeSubmitter` PR-gate double; Postgres/Temporal-backed tests use `tests/pg.py` + `tests/temporal_env.py` helpers (exercised in CI, skipped when infra absent).

Per-package test-file counts: `chemclaw` 38, `agents` 22, `workflows` 16, `mcp_servers` 12, `calc` 9, `eln` 6, `bo` 5, `kg` 5, `service` 4, `report` 3, `memory` 2, `scripts` 2, `evals` 1, `sources` 1, `workers` 1.

**Modules with NO direct test reference** (verified by cross-referencing test imports; some are indirectly exercised):
- `agents/audit_store.py` — **no direct test** (Postgres audit sink; the `AuditSink` protocol is tested via `test_audit.py`, but the Postgres backend itself is not).
- `memory/ids.py` — **no test** (id helper; likely trivial).
- `agents/memory_tools.py` — indirectly covered via `test_memory.py`.
- `calc/xtb_engine.py` — indirectly covered via `test_xtb.py`/`test_pka.py` (its two callers).
- `evals/metrics.py` — indirectly covered via the registry in `test_evals.py`.
- `scripts/validate_ord.py` — indirectly covered via `test_eln.py`/`test_eln_recipes.py`.

**Genuine coverage gaps to flag:** `agents/audit_store.py` (durable GxP audit sink — a compliance-relevant path with no direct test) and `memory/ids.py`. The `evals` package shows only 1 test file for a multi-module public surface (`metric` + `metrics` + `harness` + `ab`) — thin relative to its role as the scientific-output quality gate.

---

## 6. Provenance / Branch History

The repo was built in two clearly-dated waves, then merged. First-commit-per-directory and commit timestamps establish the mapping cleanly (the codebase is single-author, sequential — provenance is unusually legible):

### Wave 1 — Phases 0–5b (2026-07-19 to 07-20), merged via `claude/init-rxngec` PRs (#1–#4)
| Area | Introduced by | Date |
|---|---|---|
| `chemclaw/`, `agents/` (skeleton), `workflows/`, `workers/`, `infra/` | `e03d023` Phase 0 | 07-19 |
| QM Temporal spine | `baa70d5` Phase 1.1–1.4 | 07-19 |
| `calc/` (store) | `4bf2fa9` Phase 1b | 07-19 |
| `bo/` | `d95dc4a` Phase 1d | 07-20 |
| `kg/` | `533c63a` Phase 2.1–2.4 | 07-20 |
| `evals/` | `9450fec` Phase 2b | 07-20 |
| `mcp_servers/` | `d18eebf` Phase 3.1 | 07-20 |
| `eln/`, `scripts/` | `f423f85` Phase 4 | 07-20 |
| `memory/` | `0d072d4` Phase 5 | 07-20 |
| `report/` | `2503dcb` Phase 5b | 07-20 |

### Wave 2 — Foundation build F0–F7 (2026-07-21), branch `claude/agent-todo-planning-vmnwbo`, merged via `claude/merge-branches-main` (PR #8)
| Area | Introduced by | Date |
|---|---|---|
| `agents/llm_provider.py` (F0) | `40cce04` | 07-21 |
| MAF harness wiring (F1) | `8919f19` | 07-21 |
| `service/` (F2) | `57f6e82` | 07-21 |
| `agents/session_store.py`, `session_events.py`, SQL 008/009 (F3) | `7063b27`… | 07-21 |
| `service/auth.py`, `agents/authz.py`, `agents/identity/*` (F4) | `3950011`… | 07-21 |
| `workflows/hpc/nextflow.py` (F5) | `5310474` | 07-21 |
| `deploy/` OpenShift+Helm (F6) | `f0eed25` | 07-21 |
| `sources/` (F7) | `9775ca0` | 07-21 |

### Merge topology & overlapping-implementation notes
- **`claude/init-rxngec`** — Wave 1 delivery branch (PRs #1–#4). Merge `db954ad` explicitly renumbered ADRs `D-020→D-038` — evidence of a decision-log collision between branches reconciled by hand.
- **`claude/agent-todo-planning-vmnwbo`** — carried both (a) an **early** MAF Agent Harness concept (D-020, later renumbered) that landed on `main`, **and** (b) the later F0–F7 build. **This is the one area of genuine overlapping implementation:** the agent harness was implemented once as an early standalone feature and then **re-integrated a second time** as F1 (`8919f19`) on top of the F0 provider seam. Commit `db954ad` ("re-integrate agent harness onto post-5b codebase; renumber D-020→D-038") documents the manual reconciliation — the prime spot to audit for leftover dead/duplicated harness code.
- **`claude/merge-branches-main`** — final integration branch (PR #8). Commit `8468885` "Salvage role-scoped skills from phase6-authz; drop the redundant rest" is a second explicit overlap signal: a `phase6-authz` line partially duplicated the F4 RBAC/skill-gating, and only the role-scoped-skills piece was kept. Audit `agents/skill_access.py` + `agents/authz.py` for residue of the discarded implementation.
- `7628fd5` "Re-integrate the F0–F7 foundation build onto main" is the clean re-anchor of Wave 2 onto Wave 1.

**Provenance summary of overlap risk (audit priorities):**
1. Agent harness — implemented twice (early D-020 concept + F1), reconciled by hand. Check for dead code / two code paths.
2. Authz/skill-gating — a `phase6-authz` branch overlapped F4; only part was salvaged. Check for orphaned references.
3. ELN vs. generic-source registries — F7 layered `sources/registry.py` over the still-live `eln/registry.py` (two registries, overlapping).
4. ADR numbering was manually renumbered across the merge (`D-020→D-038`) — cross-references in `DECISIONS.md` may be worth spot-checking.

---

## Key flags for later audit phases
1. **`httpx` is an undeclared direct dependency** (used in 4 modules, absent from `pyproject.toml`) — pin it explicitly.
2. **SQL migration `005` is missing** from the `infra/sql/` sequence (004→006 gap) — confirm nothing was lost in the merge.
3. **Agent-harness code was integrated twice** and **authz was partially salvaged from a competing branch** — highest-value spots to hunt for duplicate/dead code.
4. **`agents/audit_store.py` (GxP durable audit sink) has no direct test** — a compliance-relevant gap.
5. **Two live name→factory source registries** (`eln/registry.py` and `sources/registry.py`) cover overlapping ground.
