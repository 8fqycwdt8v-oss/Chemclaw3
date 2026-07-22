# Phase 7 — Architecture & Boundary Audit

Repo: `/home/user/Chemclaw3` · Scope: top-level app modules (`tests/`, `.venv/` excluded).
Read-only investigation. Evidence is `file:line`.

## Executive summary

**The four-layer architecture is real and, at the import-graph level, cleanly enforced.** The
intended layering (MAF → Temporal → Skills / MCP → Git knowledge graph) is visible in the actual
dependency edges, and the two rules that matter most are both upheld:

- **No circular dependencies** exist between top-level modules. The graph is a clean DAG rooted at
  `chemclaw/` (the shared core), which imports nothing upward.
- **No lower-layer module imports `agents/`** (the MAF/LLM layer). Critically, `workflows/` never
  imports `agents/`, so LLM/non-deterministic code cannot leak into a Temporal workflow's import
  closure. Temporal determinism is preserved.

Every `@workflow.defn` file inspected wraps its heavy imports in
`workflow.unsafe.imports_passed_through()` and performs **all** I/O via `workflow.execute_activity`.
No `datetime.now()`, `random`, direct DB, or network call was found inside a workflow body.

The residual findings are narrow:

1. **Packaging incoherence** — the wheel declares only `packages = ["chemclaw"]`, but the console
   entry point is `agents.cli:main` and 12 other first-party top-level packages exist. Works today
   only because every install path is editable (repo root on `sys.path`). A real non-editable wheel
   would ship a broken `chemclaw` command and no feature code. (Medium — latent, not a live break.)
2. **`bo/` reachable by two paths** — a synchronous agent-tool path and a durable Temporal path.
   Intentional (fast propose vs. durable campaign), but worth noting as the one place a capability
   has two front doors. (Low — by design.)
3. **`calc/` cache written directly from `agents/` and `bo/` outside Temporal** — this is a
   memoization cache (D-011), not durable execution state, so it does not violate the
   "durability only in Temporal" rule. Flagged only to make the distinction explicit. (Informational.)

No layer violations, no Temporal determinism violations, no boilerplate/placeholder scaffold rot.

---

## 1. Module dependency map

Actual cross-module import edges (counts = number of `from X`/`import X` sites; built by grepping
`^(from|import)` across each module's `*.py`):

```
service   -> agents, chemclaw
agents    -> bo, calc, chemclaw, kg, mcp_servers, memory, report, sources, workflows
workers   -> chemclaw, workflows
scripts   -> chemclaw, eln, workflows
workflows -> bo, chemclaw, kg
memory    -> chemclaw, eln, kg, mcp_servers
sources   -> chemclaw, eln, report
report    -> chemclaw, kg, mcp_servers
eln       -> chemclaw, kg, mcp_servers
bo        -> calc, chemclaw
calc      -> chemclaw
evals     -> chemclaw
kg        -> chemclaw
mcp_servers -> chemclaw
chemclaw  -> (nothing upward; core only)
```

Topological layering (leaves first) — a clean DAG:

```
L0 core        chemclaw
L1 capability  calc, kg, mcp_servers, evals
L2 domain      bo, eln, report
L3 durable/mem workflows, memory, sources
L4 MAF         agents
L5 front door  service        (+ workers, scripts as alternate entry points)
```

### Circular dependencies — NONE

Verified explicitly that no lower module imports back up:

- `eln`, `kg`, `report`, `bo`, `calc`, `sources` do **not** import `memory` → no `memory` cycle.
- `eln`, `report`, `kg` do **not** import `sources` → no `sources` cycle.
- `chemclaw/` imports only `chemclaw` internally (`chemclaw -> 4 chemclaw`) — the core never
  reaches up into feature modules. This is the single most important structural property and it holds.

### Layer-violation checks (each was a specific hypothesis from the audit brief)

| Hypothesis | Result | Evidence |
|---|---|---|
| `workflows/` imports `agents/` (would break Temporal determinism) | **Clean** | No `from agents`/`import agents` anywhere under `workflows/`. |
| `chemclaw/` (core) imports upward into feature modules | **Clean** | `chemclaw` imports only `chemclaw`. |
| `service/` (front door) reaches directly into `calc/`/Postgres, bypassing `agents`/`workflows` | **Clean** | `service/` imports only `agents.*` and `chemclaw.config`. No `psycopg`, `calc`, `bo`, `kg`, or raw `execute(` in `service/`. `service/app.py:25-27`, `service/runner.py:19-20`, `service/auth.py:24`. |
| A Temporal workflow does I/O directly instead of via activities | **Clean** | Every `@workflow.defn` body uses only `workflow.execute_activity(...)`; see §3. |

**`agents -> workflows` is the correct pattern, not a violation.** `agents/qm_tools.py:21-22` and
`agents/interaction_tools.py:17` import the workflow *classes* (`QMJobWorkflow`,
`InteractionApprovalWorkflow`) and their input models purely to *start* them via the Temporal client
— the standard temporalio client idiom. The dependency direction (MAF → Temporal) matches the
intended layering; the reverse (Temporal → MAF) is what would be dangerous, and it is absent.

---

## 2. Layering coherence

**One coherent layering, no parallel/incompatible scheme introduced by branches.** The two ways
into the compute libraries are deliberately split by cost, not accidentally duplicated:

- **Fast local calculators (`calc/`)** are reached by the MAF fast path
  (`agents/calc_tools.py:10-14` → `calc.xtb`/`pka`/`solubility` + `calc.store`) and by BO objective
  evaluation (`bo/objectives.py:24-26`). This matches the plan: MAF does short reasoning + fast
  local compute; the calc cache persists each result once (D-011).
- **Heavy QM/DFT** does **not** go through `calc/` at all. The Temporal QM path
  (`workflows/qm_job.py` → `workflows/activities.py:20` → `workflows/hpc/nextflow`) reaches HPC via
  Nextflow, entirely separate from the fast `calc/` library. So there is no "two competing engines
  for the same job" problem — fast and heavy are cleanly different subsystems.

**Durability placement is correct.** State that must survive a restart lives in Temporal + Postgres,
never in MAF in-memory:

- `agents/session_store.py:31` `PostgresHistoryProvider` — conversation history in Postgres
  (`psycopg`, `session_store_dsn`/`postgres_dsn`, `session_store.py:20,46`).
- `agents/audit_store.py:24` `PostgresAuditSink` — audit log in Postgres.
- No module-level mutable dicts holding durable state were found in `agents/` (grep for
  `^_x: … = {}` / `global` returned nothing). The only per-request state is `contextvars`
  (`agents/identity_context.py`, `agents/session_context.py`), which is request-scoped, not durability.
- BO campaign state is carried as **plain data** inside the workflow (`history: list[Observation]`,
  `workflows/bo_campaign.py:49`), which replays deterministically — the correct Temporal pattern,
  not an in-memory durability leak.

**One shared coherence caveat (Low):** `bo/` (a pure library, holds no durability) is reachable two
ways — synchronously as an agent tool (`agents/bo_tools.py:20-21` → `bo.engine`) and durably as a
Temporal workflow (`workflows/bo_activities.py:13-15` → `bo.engine`). This is intentional (a quick
single-shot `propose` vs. a resumable multi-round campaign) and safe because `bo/` holds no state
and campaign durability lives in `BoCampaignWorkflow`. Noted only as the one capability with two
front doors.

---

## 3. Temporal determinism

All six `@workflow.defn` files were inspected. **No determinism violations found.**

`@workflow.defn` files: `bo_campaign.py`, `qm_job.py`, `eln_sync.py`, `report_workflow.py`,
`memory_jobs.py`, `interaction_approval.py`.

Findings per the determinism checklist:

- **Non-deterministic imports are sandboxed.** Every workflow file wraps its heavy/impure imports in
  `with workflow.unsafe.imports_passed_through():` — `bo_campaign.py:14`, `qm_job.py:17`,
  `eln_sync.py:18`, `report_workflow.py:15`, `memory_jobs.py:14`, `interaction_approval.py:22`. Only
  Temporal-safe pieces (`temporalio`, `timedelta`, `workflows.publish`) sit outside the guard.
- **All I/O is via activities.** Every side-effecting call in a workflow body is
  `workflow.execute_activity(...)`: `bo_campaign.py:43,49,61,67`; `qm_job.py:41,44,60,67`;
  `eln_sync.py:92,98,108`; `report_workflow.py:60`; `memory_jobs.py:80,94,108`;
  `interaction_approval.py:92`. No direct `psycopg`/`.execute(`, `open(`, `requests`/`httpx`,
  `os.environ`, or filesystem access appears inside any workflow.
- **No wall-clock / RNG in workflow bodies.** `datetime` imports are limited to `timedelta`
  (deterministic) for activity timeouts, plus type annotations. The two files importing bare
  `datetime` use it safely:
  - `eln_sync.py:14` — `datetime` appears only as a **type annotation** (`since: datetime`, lines
    33/52/65/71/86) and the cursor is loaded via the `load_sync_cursor` **activity**
    (`eln_sync.py:92`), not `datetime.now()` in the workflow.
  - `memory_jobs.py:10,39` — `datetime.min.replace(tzinfo=UTC)` is a **constant** (an epoch floor),
    computed at module scope, not wall-clock time.
  - No `datetime.now()`, `time.time()`, `time.sleep()`, `random.`, or bare `uuid.` call exists in any
    workflow file.
- **Retry/timeout discipline is config-driven and centralized.** `workflows/publish.py` factors the
  shared PR-gate publish + retry policy (`BAD_DATA_RETRY`, `note_publish_retry`) so the three
  publishing workflows don't drift; timeouts come from `settings.*` (`publish.py:50-52,63-66,74-76`),
  not magic numbers. Bad-data errors are non-retryable by exact class name (`publish.py:31-45`) —
  a correct Temporal-specific detail.

`bo_campaign.py` is a good exemplar: seed → N rounds of propose/evaluate, each an activity, with the
pure reductions (`best_of`, `space_exhausted`, `discrete_candidate_count`) run in-workflow on plain
data (`bo_campaign.py:56,59,74`). The module docstring explicitly narrates the determinism rationale
(`bo_campaign.py:3-7`).

**Severity: none.** This layer is exemplary.

---

## 4. Boilerplate / scaffold leftovers

**No default-scaffold rot.** Module names are all domain-meaningful (`bo`, `calc`, `eln`, `kg`,
`mcp_servers`, `workflows`, …); there are no `foo`/`bar`/`example_module`/`myapp` placeholders, no
empty "for later" packages, and `__init__.py` files are real. The layout is a deliberate flat
multi-package repo, not a `cookiecutter` skeleton.

**The one genuine packaging inconsistency (Medium, latent):**

- The build backend declares **only one** wheel package:
  `pyproject.toml:54-55` → `[tool.hatch.build.targets.wheel] packages = ["chemclaw"]`.
- But the console entry point is in a *different, undeclared* package:
  `pyproject.toml:48` → `chemclaw = "agents.cli:main"`.
- And there are **13** first-party top-level packages, as the project's own coverage config admits:
  `pyproject.toml` `[tool.coverage.run] source = ["chemclaw", "agents", "bo", "calc", "eln", "evals",
  "kg", "mcp_servers", "memory", "report", "workflows", "workers", "scripts"]`.

This is internally contradictory: a **non-editable** `pip install chemclaw-0.0.0.whl` into a clean
environment would ship only `chemclaw/` plus a `chemclaw` console script pointing at `agents.cli:main`
— which would raise `ModuleNotFoundError: agents` on first run, and none of `bo/`, `calc/`,
`workflows/`, etc. would be importable.

**Why it hasn't bitten:** every actual install path is *editable*, which puts the whole repo root on
`sys.path`, making all siblings importable regardless of the wheel declaration:

- Dev/CI: `uv sync` installs the project editable → `.venv/.../_editable_impl_chemclaw.pth` contains
  exactly `/home/user/Chemclaw3` (the repo root).
- Production image: `deploy/Containerfile:16-29` `COPY`s all 13 packages into `/app`, then
  `Containerfile:33` runs `uv sync --frozen --no-dev` (also editable, repo root `/app` on path) with
  `PATH=/app/.venv/bin` (`Containerfile:40`). The wheel's `packages` list is never exercised for
  import resolution.

So this is a **latent** incoherence, not a current runtime break. It should be resolved to match
reality — either declare all first-party packages in the wheel (the repo is genuinely a flat
multi-package project) or move shared code so the wheel is self-contained. As-is, the declared wheel
misrepresents the project and would break a real `pip install`/publish. No ADR addresses this
(searched `DECISIONS.md` for wheel/hatch/packages/namespace/flat-layout — no hit).

**Assessment:** intentional flat layout, but with an unresolved build-config bug — not "temporary
structure never renamed," rather "the packaging declaration was never reconciled with the multi-package
reality." Recommend recording the decision explicitly (flat multi-package) and fixing
`[tool.hatch.build.targets.wheel]` accordingly.

---

## 5. MCP vs. Skills boundary

**The boundary is clean and matches the intended split** (MCP = deterministic capability; Skills =
judgment):

- **`mcp_servers/` holds deterministic capability.** `mcp_servers/molfp/` and `rxnfp/` are FastMCP
  servers (`mcp_servers/molfp/server.py:10,24` `FastMCP("mcp-molfp")`) exposing pure, deterministic
  tools: fingerprint generation (`molfp/fingerprint.py:34` `ecfp_bitstring`), similarity/substructure
  search (`molfp/search.py:29,43`), backed by a shared `fpstore.py`. No LLM calls, no judgment — just
  cheminformatics functions.
- **`skills/` holds judgment.** The skill folders are decision guides, each a `SKILL.md`:
  `calculation-selection`, `experiment-design`, `qm-job-submission`, `optimization-campaign-synthesis`,
  `playbook-distillation`, `knowledge-graph-write`, `eln-reaction-extraction`, `reaction-search`,
  `deep-research`, etc. — "how do I decide X," not deterministic tools.
- **Access is mediated, not blurred.** Skills are gated by role at load time
  (`agents/skill_access.py:33,40,48` — a `SkillsSource` wrapper with per-skill role gates), separate
  from how MCP tools are wired. The two concerns don't leak into each other.

**Severity: none.** The capability/judgment separation is respected.

---

## Severity summary

| # | Finding | Severity |
|---|---|---|
| 1 | Wheel declares only `chemclaw` but entry point + 12 packages live elsewhere; works only via editable installs | **Medium** (latent; breaks a real wheel build/publish) |
| 2 | `bo/` reachable via both an agent tool and a Temporal workflow | Low (by design; `bo/` is stateless) |
| 3 | `calc/` cache written directly from `agents/`+`bo/` outside Temporal | Informational (cache, not durable execution state) |
| — | Circular deps | None found |
| — | Layer violations (service/core/workflows) | None found |
| — | Temporal determinism | None found — exemplary |
| — | MCP vs. Skills boundary | Clean |

**Overall:** the actual code matches the intended four-layer architecture to an unusually high degree.
The only concrete defect is the wheel/packaging declaration, which is masked by editable installs and
would surface only on a non-editable build — worth fixing and recording as an ADR.
