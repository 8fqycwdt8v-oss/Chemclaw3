# BACKLOG

Prioritized open action items. Top = next. Keep in sync with `docs/implementation-plan.md`
(phase/step numbers) at session end.

## Now — Phase 1: MAF + Temporal spine (first milestone)
- [ ] 1.1 Temporal worker process connects, registers on `hpc-jobs` queue (visible in UI).
- [ ] 1.2 `QMJobWorkflow` skeleton + one trivial pure activity (`prepare_input`).
- [ ] 1.3 Mock `submit_to_hpc` + `poll_hpc_status` with `activity.heartbeat()`; timeouts from config.
- [ ] 1.4 `parse_qm_output` → pydantic result model.
- [ ] 1.5 MAF agent + one skill + tool `submit_qm_job` (fire-and-forget, returns `job_id`).
- [ ] 1.6–1.10 status tool · notify callback · `background-jobs` worker · Entra `oid` audit field · QM cache.
- [ ] CHECKMATE 1 (G1–G7 + durability spike: restart worker mid-job, no completed activity re-runs).

## Done — Phase 0: foundation
- [x] 0.1 Runtime = Python; ADR-0001 (`docs/adr/0001-python-runtime.md`) + D-001.
- [x] 0.2 Tooling: uv, ruff, `mypy --strict`, pytest, pre-commit; `Makefile` `lint`/`type`/`test`/`check`/`up`.
- [x] 0.3 Central `chemclaw/config.py` (`pydantic-settings`, `extra=forbid`) + documented `.env.example`.
- [x] 0.4 Monorepo dirs with a README each: `agents/ workflows/ workers/ mcp/ skills/ knowledge/ infra/ docs/adr/`.
- [x] 0.5 `infra/docker-compose.yml`: self-hosted Temporal (dev + UI) + Postgres/pgvector.
- [x] 0.6 CI skeleton (GitHub Actions): `make check` (lint + type + test) on every push/PR.
- [x] CHECKMATE 0 (G1–G7): `make check` green; config is single source; zero unused code.

## Capability gaps to triage (from `docs/research-review.md`) — decide per item
- [x] **Evaluation / scientific-output metrics layer** → promoted to first-class **Phase 2b**
      (see plan + D-009). No longer a backlog decision.
- [ ] **Chemical/biological safety layer** — distinct from Entra-ID/RBAC (IT security).
      GxP / data-integrity + hazard checks. **Kept in backlog** (user decision); decide scope
      before any capability phase that could propose a hazardous route/procedure.
- [ ] Retrosynthesis + reaction prediction · DoE/Bayesian optimization · lab automation/SiLA2
      closed-loop · process flowsheet synthesis · multimodal analytical data · domain foundation
      models — all currently in `DEFERRED.md` with triggers; confirm or pull forward.
- [ ] Design cautions to bake in: apply Skills/tools **selectively + measured per task** (not
      universally); design the CoALA memory layer against DMR/LongMemEval, not by assumption.

## Open questions / awaiting input (see `docs/research-review.md`)
- [ ] Temporal vs. Restate/DBOS/Prefect/Dapr — no head-to-head source found; our choice stands
      on maturity/fit. Revisit if operability/cost becomes a concern.
- [ ] When does Markdown+NetworkX tip to Neo4j/Memgraph + GraphRAG? (deterministic traversal
      sidesteps the NL-query risk for now.)
- [ ] Concrete lab-automation/SiLA2 + DoE + retrosynthesis integration wiring.
- [ ] Domain safety/compliance layer design beyond RBAC.

## Later
- [ ] Phase 2 knowledge-graph core + PR-gate · Phase 3 fingerprint search · Phase 4 ELN
      ingestion · Phase 5 memory layers · Phase 5b report harness · Phase 6 identity/RBAC.
