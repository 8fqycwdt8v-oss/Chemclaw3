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
