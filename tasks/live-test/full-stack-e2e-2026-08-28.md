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
| S0 | Baseline, all repos | see below | **PASS** (4/4 repos) | `.live/baseline/*.log` |
| S1 | Four-repo bring-up | `make live-e2e-full-stack` | **PASS** after F2 | `.live/e2e-*.log` |
| S1b | Wiring check | `/readyz`, `chemclaw_connectors_unhealthy` | **PASS** | — |
| S2 | Durable path, no LLM | `make live-jobs` | pending | — |
| S3 | Template args vs live | `make live-template-args` | pending | — |
| S4 | Real-model probes | `make live-probes` | **blocked (F1)** | `tasks/live-test/transcripts/` |
| S5 | Plan gate | `make live-plan-gate` | blocked (F1), mock route TBD | `tasks/live-test/m12-plan-gate/` |
| S6 | UI full-stack | `npm run test:e2e:full-stack` | **blocked (F1)** | — |
| S7 | Storm | `make live-storm` | pending | `tasks/live-test/storm.md` |
| S8 | Corpus convergence | `make live-data`, polled | draining after F3 | `.live/e2e-corpus-backfill.log` |
| S9 | Degradation (Temporal down) | `make live-degradation` | blocked (F1), mock route TBD | `tasks/live-test/m12-degradation/` |
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
- Chemclaw3: `make lint` **PASS** · `make type` **PASS** (`mypy --strict`, **734 source files**) · `make test` **PASS** — **5753 passed, 14 skipped**, 829s, exit 0, Postgres up (`.live/baseline/cc3-test.log`).
  The 14 skips are named, not folded into the pass: **9** `helm is not installed`, **3** truncated
  git history (`fetch-depth: 0`), **2** the credential finding F1 below.
- Chemclaw3-mcp: `make check` **PASS**, exit 0 — ruff clean, `mypy --strict` clean over **120 source files**, **1521 passed / 7 skipped** in 334s, `pip-audit`: no known vulnerabilities (`.live/baseline/mcp-check.log`)
- Chemclaw3_ui: `typecheck` **PASS** (exit 0) · `lint` **PASS** (exit 0) · `test` **PASS** — 74 files, **761 tests passed**, 40.1s (`.live/baseline/ui-*.log`)
- Chemclaw3_mock: `pytest` → **PASS**, 39 passed, exit 0 (`.live/baseline/mock-test.log`); venv built at `/home/user/Chemclaw3_mock/.venv`, which the lane needs for `mock-eln`/`mock-vendor`

## Findings

### F1 — the model credential is present but unfunded (blocks 4 of 11 stages)

**Measured twice, independently.** The baseline surfaced it as two skips in
`tests/test_prompt_caching.py`, and a direct call confirms it:

```
POST https://api.anthropic.com/v1/messages  ->  http_status=400
{"type":"error","error":{"type":"invalid_request_error",
 "message":"Your credit balance is too low to access the Anthropic API. ..."},
 "request_id":"req_011CeVpRiCPHUynVbPAgmywp"}
```

So `API-KEY` exists and `up.sh` maps it (it only asserts the variable is non-empty, line 267),
but **no real turn can complete**. This is a lane/account fact, not a code defect — there is
nothing in any of the four repositories to fix, and it is the user's call to resolve.

Caught at baseline rather than at S4 after a two-hour bring-up, which is what the baseline is for.

**Consequence for the stage table**, split by what each stage actually measures:

- *Blocked on real-model behaviour* — S4 probes and S6 UI full-stack. Both score what a real
  model chose to do (`full-stack.spec.ts`: "did the turn call `predict_pka` and cite its result").
  A mock cannot stand in without measuring something else and calling it the same name.
- *Possibly recoverable against the mock* — S5 plan gate and S9 degradation. Both measure
  **harness mechanics** (plan -> approve -> execute -> re-gate; `capability_degraded` precedes the
  first token), not model quality. `core/config/llm.py` has an `openai_compatible` provider with
  `llm_base_url`, and `cli/mock_llm.py` already serves one on :8820 for the storm. Whether the
  lane supports pointing the front door at it is the next tick's question.
- *Unaffected* — S1, S1b, S2, S3, S7, S8, S10. The durable path, the manifest checks, the storm
  and the soak all run with **no LLM call at all** by design.

## What this run is not evidence about

Stated up front so the final report cannot imply otherwise: the live OpenShift cluster,
`helm`/`kubeconform` render against a real API server, and the browser→Entra tenant hop (MSAL talks
to `login.microsoftonline.com`; mocking that is mocking a login UI, not a key set). All three stay
open edges in `docs/planning/BACKLOG.md` and cannot be closed by this lane.

### F2 — the four-repo lane could not start its own front door

**Symptom.** `up.sh up` exits 1 at `api exited before becoming ready`. Every other process comes
up: mock-eln, mock-vendor, props, rxnpredict, calc, chem, safety, connectors, and all four Temporal
workers report ready. Only the front door dies, and the lane's own message names a log rather than
a cause.

**Root cause**, four lines deep in `.live/api.log`:

```
WARNING chemclaw.connectors.health: connectors unreachable at startup:
        pyexec (unreachable: ConnectError: All connection attempts failed)
ERROR   chemclaw.connectors.health.ConnectorsUnavailable: connectors_required is set but
        these connectors are unreachable: pyexec (unreachable)
```

`up.sh:268` puts `$MCP_REPO/manifests` on `CHEMCLAW_CONNECTORS_DIR`. That directory holds **five**
manifests — `chem`, `props`, `pyexec`, `rxnpredict`, `safety` — so `pyexec` is *discovered*, and
its manifest carries its own loopback default (`http://127.0.0.1:8899/mcp`), so the front door
health-checks it. But **no lane script starts it**: `up.sh` starts props, rxnpredict, calc, and the
two mocks; `processes.sh` starts chem and safety. `grep pyexec` over both scripts matched nothing.
Under `CHEMCLAW_CONNECTORS_REQUIRED=true` an unreachable discovered connector is fatal at startup
rather than degraded at call time, so the front door refused to boot.

This is a **cross-repo coupling failure**, not a typo: adding `manifests/pyexec/` in Chemclaw3-mcp
silently broke Chemclaw3's four-repo lane, because the manifest directory is the contract and
nothing ties "a manifest is on the path" to "something serves it". The lane has no test — it is a
manual lane by design — so the first thing to notice was a dead front door two hours into a
campaign.

**Fix** (this repo, `infra/live/e2e-full-stack/`): `start_pyexec()` mirroring `start_props`, called
beside its peers during bring-up, added to the `restart <name>` set, and a row in the README's
process table. The alternative — narrowing `CHEMCLAW_CONNECTORS_DIR` to exclude it — was rejected:
the lane exists to exercise every advertised capability, and a front door advertising fewer tools
than a real deployment is the wrong thing to measure.

**S1 re-run, after F2** — `bringup2 exit=0`. `pyexec started` / `pyexec ready` /
`pyexec credential accepted (HTTP 406)`, then `api ready` and `full stack up`.
(406 is the expected answer: an MCP endpoint reached without the streaming Accept header, which is
what `assert_credential_accepted` checks for — it proves the credential was taken, not refused.)

**S1b** — the wiring check the lane README insists on, because absence of an error is not success:

```
$ curl -s localhost:8000/readyz     -> {"status": "ready", "connectors_unhealthy": 0}
$ curl -s localhost:8000/metrics    -> chemclaw_connectors_unhealthy 0
```

### F3 — the corpus backfill could never find its ground truth

**Symptom.** Bring-up ends with `WARNING: corpus backfill failed`, and **exits 0 anyway**. The ORD
half of the corpus is then permanently invisible, which is precisely the failure `cli/live_data.py`
was written to detect in the data — its own module docstring says a previous run "reported 638 note
proposals and called ingestion proven" while 57% of the corpus had never entered the system.

**Root cause.** `live_data.py` derived the published factor tables by walking up from the ORD export
directory with three `.parent`s. The lane sets `ord_export_dir` to
`<mock repo>/data/eln/exports/ord` — **four** levels below the repo root — so the derivation landed
on `<mock repo>/data` and produced `<mock repo>/data/app/eln/real_data`, a path that has never
existed. The tables are at `<mock repo>/app/eln/real_data`. The lane's own error message named the
correct path while the code kept computing the wrong one.

It survived because the derivation was an expression inside `main()`: `tests/test_live_data.py`
existed and had **no** reference to `real_data` at all, because nothing could reach the arithmetic
without running the whole lane against a seeded checkout.

**Fix** (this repo, `cli/live_data.py`): extracted `_default_real_data()` — the unit that was
untestable — corrected to `parents[3]`, with two tests proven red against the pre-fix code.

**And the fix's own first version was wrong**, which is worth recording rather than quietly
amending. `parents[3]` raised a bare `IndexError: 3` on the *shipped* default
(`ord_export_dir = "data/eln-exports/ord"` — relative, three parts), so every invocation outside
this lane crashed from inside `pathlib` naming neither the bad setting nor the flag that fixes it.
My two tests both passed, because I wrote them from the same understanding of the layout that
produced the off-by-one — the exact pattern `tasks/lessons.md` records for tests written alongside
their own change. The helper now returns `None` for an underivable path and `main` reports which
setting it could not derive from. Two further tests cover that domain.

**Verified**: the backfill now starts — `eln-backfill-epoch: still draining`, the workflow running
on the broker, which is S8's long pole and expected to take over two hours.
