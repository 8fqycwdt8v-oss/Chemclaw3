# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Phases 0–5b of the plan are **implemented and CHECKMATE-reviewed**: toolchain + config,
the MAF+Temporal spine, fast calculators (xTB/pKa/solubility) with the Postgres
calculation cache, BoFire BO campaigns, the knowledge graph + PR-gate, the eval/metric
layer, ECFP4/DRFP fingerprint search, ELN ingestion, the memory layers, and the report
harness.

The **foundation build F0–F7** (the real target stack: OpenShift + HPC/Nextflow + an internal
OpenAI-compatible LLM, Entra identity system-wide) is **implemented for everything verifiable
offline**, each phase ADR'd (D-038…D-049) and green under `make lint type test`:

- **F0** LLM provider seam (generic credential, not Entra) · **F1** MAF harness (plan/execute) ·
  **F2** FastAPI+SSE front door · **F3** durable Postgres sessions + job→session push-back.
- **F4** Entra identity/RBAC: front-door OIDC, one authorization gate, `require_actor` reject-if-absent
  core rule, workload identity federation, OBO (dormant), Temporal-mTLS + HPC identity bridges.
- **F5** real Nextflow (Seqera/Tower) launcher behind the QM activities (mock kept for CI).
- **F6** OpenShift delivery: one rootless image, Helm chart, CI, three-secret model, Temporal self-hosted.
- **F7** the generic `DataSource` seam (`sources/`) — ELN re-hosted unchanged; a new source is one
  registry entry + one config token. First live connector (deferred): a custom Snowflake ELN source.

**Live edges remain open** (need a real Entra tenant / Temporal broker / OpenShift cluster): real token
validation, federation/OBO exchanges, live cluster durability + `helm`/`kubeconform` render. See
`BACKLOG.md` for the exact list. The design and staged build order remain the source of truth:

- `docs/architektur.md` — the four-layer architecture (§6 = the real OpenShift/Nextflow/internal-LLM
  deployment; §7/§8 = Entra durchgängig).
- `docs/implementation-plan.md` — the original build order; `docs/implementation-tickets.md` — the
  F0–F9 ticket backlog with per-phase status.

## Architecture (the one thing to internalize)

Four layers, each with a single responsibility. **Never merge their concerns.**

1. **MAF** (Microsoft Agent Framework) — conversation orchestration + short reasoning steps.
2. **Temporal** — durable execution of long/expensive jobs. Early focus is fast local compute
   (xTB/GFN2, ML predictors) + BoFire BO; **HPC/DFT is deferred** (D-010). Two task queues:
   `hpc-jobs` (few, heavy workers) and `background-jobs` (light workers: sync, re-index, reports).
   Every result is persisted once via the calculation store — never recomputed (D-011).
3. **Agent Skills** (`SKILL.md`) — "how do I do X" (judgment), loaded on demand.
4. **Markdown knowledge graph in Git** (NetworkX indexer) — "what do we know" (data + relations).

Durability lives **only** in Temporal, never in MAF. Skills hold judgment; MCP servers hold
capability (deterministic tools). Anything agent-generated enters the graph via a **PR-gate**
(human validates before merge) — this is the GxP "AI proposes, human signs off" line, reused
everywhere (job results, reports, distilled playbooks). See `docs/architektur.md` §4, §9, §12.

## Commands

The toolchain is fixed by the plan (Phase 0) but not yet scaffolded. Once it exists, use the
`Makefile`/`justfile` targets rather than raw invocations:

- `make lint` — ruff (lint + format). `make type` — `mypy --strict`. `make test` — pytest.
- `make up` — `docker-compose` (self-hosted Temporal dev cluster + Postgres/pgvector).
- Single test: `pytest path/to/test_file.py::test_name` or `pytest -k "name substring"`.

A step is done only when its acceptance check passes **and** `make lint type test` is green.

## Code quality (non-negotiable)

- **Perfection over speed**: when unsure, ask — do not guess.
- **KISS**: simplest working solution; no over-engineering. No abstraction without a second
  real caller (Rule of Three); an abstraction with one caller gets inlined.
- **DRY**: no duplicate logic — extract shared code. The PR-gate and the retriever interface
  are single reusable pieces, not copy-paste.
- **No boilerplate**: only code that is actually used. Delete dead params, empty interfaces,
  and "for later" stubs on sight.
- **Docstrings on every module/function**: state the *purpose* and the *why*, not just the what.
  Every public function is fully type-annotated.
- **Small, single-responsibility, clearly named functions.**
- **After every change**: run existing tests, add tests where they prove behavior (not mocks).
- **Config, never magic numbers**: every URL, path, threshold, timeout, model name comes from
  the one `pydantic-settings` config, ENV-overridable.

Run the plan's **Quality-Gate ("Checkmate")** checklist (G1–G7, see `docs/implementation-plan.md`)
after each cluster of steps before moving on.

## Persistent knowledge (read at session start, update at session end)

- `BACKLOG.md` — prioritized open action items.
- `DEFERRED.md` — consciously postponed items **with the reason they are not now**.
- `DECISIONS.md` — architecture decisions with rationale (append-only ADR log).

Keep these current; they are the memory across sessions. For recurring patterns, prefer a
`.claude/skills/<name>/SKILL.md` over bloating this file.

## Token / context management

- **Compact policy** — when context is compacted (`/compact`), the summary MUST preserve:
  open TODOs (from `BACKLOG.md`), API/interface changes **with their rationale**, the list of
  changed files, and a one-line summary of any failed approach (so it is not retried).
- After finishing a self-contained step, actively suggest/use `/compact` (or `/clear`).
- Keep replies as short as possible; no explanations without added value.
- Use **subagents** for exploration/verification so failed attempts never accumulate in the
  main context (subagents have their own context window and tools).

## Governance

Treat this file like code: version it, review changes in a PR, and re-test it in a fresh
session before merge. Do not duplicate anything already in `README.md` or a package manifest.
