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

## Open questions / awaiting input
- [ ] Fold findings from the deep-research architecture review into the docs once it completes
      (watch for retrosynthesis, DoE/Bayesian optimization, lab automation / SiLA2 gaps).

## Later
- [ ] Phase 2 knowledge-graph core + PR-gate · Phase 3 fingerprint search · Phase 4 ELN
      ingestion · Phase 5 memory layers · Phase 5b report harness · Phase 6 identity/RBAC.
