# Task: make the repository navigable for a human

Requested 2026-07-29: "the GitHub now looks quite messy … many many folders with an unclear
structure and with often similar names. Is there a way to bring more structure into the git?"

Branch: `claude/github-repo-structure-6yyibc`. Three ADRs: D-145, D-146, D-147.

## What was actually wrong

1. `services/chemclaw/` — a vestigial tier from the abandoned Replit monorepo whose other half was
   already deleted. It forced `working-directory:` into every workflow, and had already cost this
   repository months of `main` with no CI at all (D-117), because the gates sat *inside* the wrapper
   where GitHub Actions does not read.
2. Eighteen flat top-level packages with no stated grouping, several near-homonyms of each other:
   `chemclaw/` inside `Chemclaw3` (naming neither, but the kernel), `calc/` beside
   `connectors/calc/`, `workflows/` beside `.github/workflows/`, `workers/` beside
   `connectors/*/worker.py`, `service/` one character from `services/`.
3. `DECISIONS.md` at 421 KB with 134 ADRs in one append-only file, `BACKLOG.md` at 124 KB, and a
   second near-empty ADR mechanism in `docs/adr/`.
4. No repository-root README: a visitor saw `.github/` and `services/`.

## Stage 1 — flatten (D-145) — DONE

- [x] `git mv` every entry of `services/chemclaw/` to the root; delete the tier
- [x] Merge the two `.gitignore`s; repoint the Git-LFS rule at `.bin/temporal`
- [x] Drop `working-directory` from both workflows
- [x] Add `ARCHITECTURE.md` — every directory mapped to a layer, and the two name pairs that look
      like duplicates and are not
- [x] Leave past-tense `services/chemclaw/` mentions in the append-only record alone

## Stage 2 — the record (D-146) — DONE

- [x] Split `DECISIONS.md` into `docs/decisions/D-NNN-<slug>.md`, verified byte-identical per ADR
- [x] Fold `docs/adr/` into D-001 and delete it; `ADR-REGISTRY.md` → `docs/decisions/README.md`
- [x] `docs/` → `decisions/ planning/ guides/ reference/ archive/`, with `docs/README.md` saying
      which are maintained
- [x] Rewrite `tests/test_decision_log.py` for the new shape; update CLAUDE.md's allocation procedure
- [x] Update references across 73 files

## Stage 3 — regroup (D-147) — DONE

- [x] 18 packages → `src/chemclaw/{core,agent,api,durable,connectors,science,kg,ingest,retrieval,memory,mcp,templates,evals,cli}`
- [x] ~1200 imports plus the module paths in strings, manifests, entrypoint, Helm, Makefile, CI
- [x] Code/data split; `eln/exports` → `data/eln-exports`
- [x] `connectors_dir` / `data_sources_dir` / `safety_rules_path` resolve against the package
- [x] Rewrite `test_packaging.py`; extend `test_layering.py` with "the kernel imports no sibling"
- [x] `.git-blame-ignore-revs` for all three restructure commits

## Stage 4 — what CI caught that the offline gate did not — DONE

- [x] `test_image_ships_every_first_party_package` went **vacuous**: it discovered root packages,
      and D-147 left none, so it iterated an empty set and passed. Replaced with an assertion that
      `src/` is COPYd, plus a widened runtime-data check and an exists-on-disk check.
- [x] `make db-migrate` applied **zero migrations, silently**. `_read_sql_files` located `infra/sql`
      as `__file__`'s grandparent — the repo root only while the module was `calc/migrate.py`.
      `glob` on a missing directory raises nothing, so a wrong path is indistinguishable from a
      migrated database. Now `settings.sql_migrations_dir`, with `tests/test_migrations.py`.
- [x] Three module paths the import sweep missed because they are not imports: the Helm
      migrate-Job's command, the runbook's connector command, four `deploy/README.md` entries.

## Review

**Verification.** `make lint type cov` green (1556 passed, 83% over 206 measured modules — every
first-party module, checked against the file count so nothing dropped out of measurement). All eight
declaration validators pass, which is the only thing that proves the `module:callable` strings in
`connector.yaml` and `datasource.yaml` were repointed, since mypy cannot see them. Every component
`deploy/entrypoint.sh` dispatches imports. The front door was started with its working directory
*outside* the repository and discovered all seven connector bundles — behaviour the old
CWD-relative defaults could not deliver. `git log --follow` still reaches each moved file's history.

**What went wrong, and the pattern.** Both Stage 4 items are the same failure: a mechanical refactor
survived every offline gate because the broken thing **fails by finding nothing** rather than by
raising. An empty `glob` and an empty discovery loop both read as success. Type checking cannot see
either, and a test that iterates a now-empty set reports green while asserting nothing.

The rule for the next large move: after a rename, grep for `for x in <discovery>()` in both source
and tests, and check that the discovery is still non-empty. Emptiness is the failure mode a rename
produces, and it is the one nobody writes an assertion for.

**Not done, deliberately.** `science/calc` was not merged into `connectors/calc` (nor `bo`,
`safety`, nor `mcp/` into `connectors/`): the engine/wrapper split is the layering rule, and merging
would put Temporal imports inside the physics. The names hid the split; the split was never the
problem. `skills/` was not merged with `connectors/*/skills/`: a bundled skill deploys with its
connector, a global one does not. Both are recorded in `ARCHITECTURE.md` so the next reader does not
have to re-derive them.

**Still open.** `.github/workflows/image.yml` runs only on `main` and on pull requests, so the
Containerfile change has not been executed by CI on this branch — `tests/test_deploy_chart.py`
checks its COPY set offline, but only a real build proves the closure installs and imports under an
arbitrary non-root UID. Opening a pull request triggers it.
