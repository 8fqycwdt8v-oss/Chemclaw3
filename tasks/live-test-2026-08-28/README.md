# Live e2e campaign — 2026-08-28

A multi-hour pass over the three-repo stack (`Chemclaw3` + `Chemclaw3-mcp` + `Chemclaw3_ui`) driven
entirely by `chemclaw.cli.mock_llm`. **No live LLM call is made anywhere in this campaign**, and the
claim is checked rather than asserted — see *Verification* below.

## Posture

`Chemclaw3_mock` is **not** in this session, so the lane runs its three-repo posture. The following
are **NOT RUN** — not passed, not skipped-green:

| Out of scope | Why |
| --- | --- |
| `mock-vendor` (`search_building_blocks`, `get_price`) | served by Chemclaw3_mock |
| the seeded ELN/ORD corpus and `make live-data` | its exports live in Chemclaw3_mock |
| the Entra-enforced posture | the mock tenant is Chemclaw3_mock's `app/entra/` |
| UI full-stack scenarios 4 and 5 | they assert against the two rows above |
| `make helm-validate` | no `helm`, `kubeconform` or `promtool` in this container |
| `make live-probes`, `make eval*`, `live-verifier-margin` | they grade answers, and a scripted model emits tool calls without *choosing* them — both arms would measure the script |

## Findings

Each was found by running the thing, root-caused, fixed with a test or a live re-measurement, and
merged. All four turned out to be one shape; see
`docs/decisions/D-2026-08-28-a-lane-primitive-must-verify-the-act-it-was-asked-for.md`.

| # | Finding | Fix |
| --- | --- | --- |
| 1 | `restart-postgres` restarted nothing on a Docker lane while logging that it had; the storm's E3 check scored PASS over a bounce that never happened | compose branch for both Postgres verbs; the check now reads `pg_postmaster_start_time()` either side and fails if it did not move |
| 2 | The lane started five of six published fleet manifests; the front door refused to boot on the missing `pyexec` | the start list is derived from the manifests actually mounted |
| 3 | `processes.sh env`, the documented contract for a second shell, carried the minted credentials and not the resolved fleet checkout — every chaos restart died | the contract carries `CHEMCLAW_MCP_REPO` |
| 4 | The next restart killed the front door, declined to start it for want of a model posture the contract also omitted, printed "live stack up" and exited 0 | the contract carries the model posture; `restart <name>` asserts `<name>` came back |
| 5 | The F-family check *"a truncated argument document is reported, not swallowed"* could never pass: LangChain's `parse_partial_json` closes the string, so truncation never reaches the repair path — a fact `agent/model_calls.py` documents | split into `f-malformed-json` (genuinely unparseable, exercises the repair path) and `f-truncated-arguments` (pins that a cut document is completed and *run*). Family F now 9/9 |
| 6 | An unparseable tool call surviving its repair reached a metric and a log and **nothing on the chemist's stream** — `answered=False, tools_failed=[]`, a blank box with no cause | `_report_repair` publishes a `ToolFailureSignal` per unrepaired call, with `reason=None` (no gate refused it; it is a fault) |
| 7 | **Every durable calculation returned HTTP 401 behind a `/readyz` reporting `connectors_unhealthy: 0`** — `calc` is a backend, so the health probe structurally cannot see it. The contract carried the four fleet tokens it *minted* and dropped the four it *inherited*; `CHEMCLAW_CALC_MCP_TOKEN` was present and is a different variable one letter away | `fleet_token_vars` derives the set from the mounted manifests; verified with `env -i` + contract only + a real xTB job, 5/5 |
| 8 | **The soak ran five rounds, reported `24/24 checks passed` each time, and wrote nothing.** The whole Prometheus exposition went through the environment at an exec boundary; past `MAX_ARG_STRLEN` the exec fails and no line is written. The record is the only artefact a soak produces | both scrapes go to `mktemp` files; `json.dumps` still builds the line. Third distinct way this script has lost its own record |

### Check bugs found in the campaign's own instruments

Recorded separately because they are not product defects, and reporting them as such would have been
the campaign's worst possible output.

| Instrument | What it claimed | What was true |
| --- | --- | --- |
| Family `T` dry-run checks (×14) | `refused=0` — the dry-run control did not fire | The control fires. A refusal is *raised*, so it arrives as `tool_failed` and the error `ToolMessage` is suppressed; the check read `result_previews`, which is necessarily empty for a raised refusal. `TurnResult.failure_messages` added |
| My own reachability probe | "a default-profile turn reaches 17 of 99 tools; 83 unreachable" | The default profile advertises **91 of 99**. I parsed the tool list out of the model's error string, which is truncated at 300 characters without closing its bracket |
| UI fixture tier | 40 failures | A bare `npm run build` produces a client that refuses `AUTH_MODE=dev`; the failure mode is 40 × 30s `locator.click` timeouts with the cause buried in `[WebServer]` output. Rebuilt with the flag: 6 failed, 65 passed, 57s |
| Storm families A and E (first run) | noise 26%, lease 10.4s | Measured against a box a subagent was loading. Re-measured idle: still 21% noise — so the A rows are honest, and the campaign's own rule (never share the box with a storm) was violated twice before it stuck |

## Suite results

| Suite | Result |
| --- | --- |
| `Chemclaw3` full pytest, Postgres **and** Temporal up | 5,751 passed, 14 skipped (6 helm, 3 truncated git history, 2 no model credit) — the Postgres- and Temporal-gated files ran rather than skipping |
| `Chemclaw3-mcp` `make check` | 1,521 passed, 7 skipped; mypy clean over 120 source files; no known vulnerabilities |
| `Chemclaw3-mcp` `make offline-run` | 1,518 passed **with the network namespace removed** — the vendored corpora are provably sufficient |
| `Chemclaw3_ui` vitest | 772 passed over 75 files |
| `Chemclaw3_ui` Playwright fixture tier | 65 passed, 6 failed — all one pre-existing `transcript.spec.ts` assertion on `main` |
| `Chemclaw3_ui` Playwright mock-model tier (new) | 4 passed, 4 failed on first live run; failure 4 was finding #7 above |

## Runs

| Wave | Artefact | Verdict |
| --- | --- | --- |
| 1 · durable smoke | `live-jobs.log` | 5/5 — a real GFN2-xTB reaction energy through Temporal, cached, deduped, wedged-worker pending |
| 2 · storm, 8 families | `storm-wave2.md` | see the report |

## Verification

1. **Zero live LLM calls.** `GET :8820/__mock/stats` reconciled against the turn count, plus
   `ANTHROPIC_API_KEY` unset in every process the lane starts. Independently, this environment's
   `API-KEY` is present but its account has no credit (`invalid_request_error: credit balance is
   too low`), so a live call could not have succeeded even by accident.
2. **Coverage run vs planned.** The storm declares `FAMILIES` and names the difference itself; the
   NOT RUN table above is the rest.
3. **What drifted.** `.live/soak.jsonl` and `make live-soak-report`.

## Drift, over the first 8 recorded soak rounds

Readable only after finding #8 — before that fix the record was empty while every round reported
success. Full fit in `soak-report-r8.md`; `rounds with a non-zero exit: none`.

| Series | Round 1 | Round 8 | Fit |
| --- | ---: | ---: | --- |
| api RSS (KB) | 588,732 | 607,032 | **grows and slowing** — +5,414 then +1,202 KB/round (± 560). Decelerating is the caches-filling signature, not a leak; 8 rounds is a baseline, not a verdict |
| `chemclaw_pg_pool_size` / `_available` | 13 | 16 | grows and steady, max 48, `requests_waiting` flat at 0 |
| `calculation_results` rows | 15 | 15 | **flat across 279 turns** — D-011's "a persisted result is never recomputed", visible as a number |
| `session_turns` rows | 8 | 8 | flat, while `session_messages` grows +144/round — turn rows reclaimed, messages retained |
| `audit_events` rows | 2,937 | 3,539 | +86/round, steady |
| `turn_costs` rows | 1,435 | 1,659 | +32/round — one per turn started, as expected |
| turns started / failed / empty | 55 / 20 / 18 | 279 / 90 / 81 | +32 / +10 / +9 per round; the ~31% failure rate is families F and H doing their job |
| disk free | 20 GB | 20 GB | flat |

The container was reclaimed at round 8 and **the record survived** — which is the whole reason the
soak is checkpointed per round rather than held in a process.
