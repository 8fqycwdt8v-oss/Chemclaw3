# BACKLOG

Prioritized open action items. Top = next. Keep in sync with `docs/implementation-plan.md`
(phase/step numbers) at session end.

## Now — Phase 0: foundation
- [ ] 0.1 Fix runtime = Python; record as ADR-0001 in `DECISIONS.md` (already D-001).
- [ ] 0.2 Tooling: uv/poetry, ruff, `mypy --strict`, pytest, pre-commit; `Makefile`/`justfile`
      with `lint`, `type`, `test`, `up`.
- [ ] 0.3 Central `config.py` (`pydantic-settings`, ENV override) + documented `.env.example`.
- [ ] 0.4 Monorepo dirs as empty packages with a README each: `agents/ workflows/ workers/
      mcp/ skills/ knowledge/ infra/ docs/adr/`.
- [ ] 0.5 `infra/docker-compose.yml`: self-hosted Temporal (dev) + Postgres/pgvector.
- [ ] 0.6 CI skeleton (GitHub Actions): lint + type + test on every push.
- [ ] CHECKMATE 0 (G1–G7) before starting Phase 1.

## Next — Phase 1: MAF + Temporal spine (first milestone)
- [ ] Steps 1.1–1.10 (see plan). HPC mocked; prove worker-restart durability at CHECKMATE 1.

## Capability gaps to triage (from `docs/research-review.md`) — decide per item
- [ ] **Evaluation / scientific-output metrics layer** — benchmarks (step count, time-to-in-vitro)
      + green-chemistry metrics (E-Factor, PMI). We gate *code* quality but not *scientific*
      output. Strong candidate to make first-class (pairs with "tools aren't uniformly good").
- [ ] **Chemical/biological safety layer** — distinct from Entra-ID/RBAC (IT security).
      GxP / data-integrity + hazard checks. Decide scope.
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
