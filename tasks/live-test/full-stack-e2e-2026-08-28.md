# Four-repo end-to-end campaign — 2026-08-28

Driven by a self-paced `/loop`. **This file is the loop's state**: each tick reads it to find the
current stage, advances exactly one stage (or polls one long-running stage), and writes the verdict
back here. That is what makes the campaign survive the ~hourly container reclaim that
`infra/live/soak.sh`'s header records killing a scratch loop at round 5 of 200.

Every verdict below resolves to an HTTP status, a row count, a Temporal workflow state, a declared
metric, or an event written to disk — `live_storm.py`'s standing rule and the D-2026-08-03
correction behind it. Nothing is scored from prose.

## Lane facts

- Repos on disk: `/home/user/Chemclaw3`, `Chemclaw3-mcp`, `Chemclaw3_ui`, `Chemclaw3_mock` (cloned
  this session; it is the fourth repo `up.sh` needs for `mock-eln`:8090 and `mock-vendor`:8091).
- Every `up.sh` call carries `CHEMCLAW_MCP_REPO` / `CHEMCLAW_MOCK_REPO` / `CHEMCLAW_UI_REPO`,
  because the defaults point at `/workspace/...` which does not exist here.
- Host: 4 CPUs, 15 GB RAM, ~30 GB free. `soak.sh` stops itself below a 4 GB disk floor.
- Model credential present as `API-KEY`; `up.sh` maps it to `ANTHROPIC_API_KEY` itself.

## Stage ledger

Status: `pending` · `running` · `PASS` · `FAIL` · `skipped (reason)`

| # | Stage | Command | Status | Evidence |
| --- | --- | --- | --- | --- |
| S0 | Baseline, all repos | see below | running | `.live/baseline/*.log` |
| S1 | Four-repo bring-up | `make live-e2e-full-stack` | pending | `.live/e2e-*.log` |
| S1b | Wiring check | `/readyz`, `chemclaw_connectors_unhealthy` | pending | — |
| S2 | Durable path, no LLM | `make live-jobs` | pending | — |
| S3 | Template args vs live | `make live-template-args` | pending | — |
| S4 | Real-model probes | `make live-probes` | pending | `tasks/live-test/transcripts/` |
| S5 | Plan gate | `make live-plan-gate` | pending | `tasks/live-test/m12-plan-gate/` |
| S6 | UI full-stack | `npm run test:e2e:full-stack` | pending | — |
| S7 | Storm | `make live-storm` | pending | `tasks/live-test/storm.md` |
| S8 | Corpus convergence | `make live-data`, polled | pending | `.live/e2e-corpus-backfill.log` |
| S9 | Degradation (Temporal down) | `make live-degradation` | pending | `tasks/live-test/m12-degradation/` |
| S10 | Soak + drift | `make live-soak`, `make live-soak-report` | pending | `.live/soak.jsonl` |

**Sequencing that matters.** S1 starts the ORD corpus backfill, which drains for 2h+ — so S2–S7 run
over the top of it and S8 is *polled*, never blocked on. S9 requires Temporal **stopped**, so it
runs after everything needing Temporal up, and infra is restarted behind it before S10.

## S0 — baseline (before any edit)

`tasks/lessons.md` (2026-08-28): the baseline is the only artefact that can tell "my change broke
this" from "this was already broken". `make test` runs with Postgres up — without it the suite
skips a large set and still prints green.

- Infra: `make live-infra` up (postgres, temporal, temporal-ui containers healthy); `make db-migrate`
  applied **71 migrations**, `converted 0 stored message(s)`.
- Chemclaw3: `make lint` **PASS** (exit 0, 734 files formatted) · `make type` **PASS** (exit 0, `mypy --strict` clean over **734 source files**) · `make test` → running, Postgres up
- Chemclaw3-mcp: `make check` **PASS**, exit 0 — ruff clean, `mypy --strict` clean over **120 source files**, **1521 passed / 7 skipped** in 334s, `pip-audit`: no known vulnerabilities (`.live/baseline/mcp-check.log`)
- Chemclaw3_ui: `typecheck` **PASS** (exit 0) · `lint` **PASS** (exit 0) · `test` **PASS** — 74 files, **761 tests passed**, 40.1s (`.live/baseline/ui-*.log`)
- Chemclaw3_mock: `pytest` → **PASS**, 39 passed, exit 0 (`.live/baseline/mock-test.log`); venv built at `/home/user/Chemclaw3_mock/.venv`, which the lane needs for `mock-eln`/`mock-vendor`

## Findings

None yet.

## What this run is not evidence about

Stated up front so the final report cannot imply otherwise: the live OpenShift cluster,
`helm`/`kubeconform` render against a real API server, and the browser→Entra tenant hop (MSAL talks
to `login.microsoftonline.com`; mocking that is mocking a login UI, not a key set). All three stay
open edges in `docs/planning/BACKLOG.md` and cannot be closed by this lane.
