# The four-repo end-to-end lane

Brings up this backend together with the three companion repos so a chemist's turn actually
crosses every boundary the architecture claims it can: a real browser, a real LangGraph turn
against a real Anthropic model, a real MCP connector fleet, and
mocked ELN/ORD data sources. Nothing here runs in `make ci` — like the rest of `infra/live/`, it
is a manual lane run against a checkout, not a diff.

Closes the gap `tasks/todo.md` used to name: *"the cross-repo sequence `Chemclaw3_mock` →
`Chemclaw3` → `Chemclaw3_ui`... not started."*

## What comes up

| Process | Repo | Port | Started by |
| --- | --- | --- | --- |
| Postgres/pgvector + Temporal | this repo | 5432, 7233 | `infra/live/bootstrap.sh` |
| PR-gate note repo | this repo | — | `infra/live/bootstrap.sh` |
| `props` (solvent/pure-component properties) | Chemclaw3-mcp | 8850 | this script |
| `rxnpredict` (forward/condition prediction, `fake_a`/`fake_c` doubles) | Chemclaw3-mcp | 8857 | this script |
| `chem` (RDKit: resolve, stoichiometry, green metrics, render) | Chemclaw3-mcp | 8858 | `infra/live/processes.sh` |
| `safety` (structural hazard / genotoxicity screen, ICH limits) | Chemclaw3-mcp | 8859 | `infra/live/processes.sh` |
| `calc` (the physics behind this repo's calculator tools — *not* a connector) | Chemclaw3-mcp | 8860 | this script |
| `mock-eln` (ELN/ORD data) | Chemclaw3_mock | 8090 | this script |
| `mock-vendor` (building-block search/pricing MCP tool) | Chemclaw3_mock | 8091 | this script |
| connectors, 4 Temporal workers, front door | this repo | 8810+, 9000-9003, 8000 | `infra/live/processes.sh` |
| BFF + SPA | Chemclaw3_ui | 8787, 5173 | this script |

**`chem` and `safety` are started by `infra/live/processes.sh`, which this script calls** — one
lane starts them, and it is the one that cannot boot without them
(`docs/decisions/D-2026-08-27-one-lane-starts-the-fleet.md`). Both scripts used to start them, and
because a pidfile is a per-lane record of a machine-wide port the duplication was silent: the
second uvicorn died on the bound address while readiness was answered by the first, so every
four-repo bring-up left two dead pidfiles and `make live-e2e-full-stack-status` printed `chem DOWN`
directly above `chem up`. This script still *checks* their credential after `processes.sh` returns
— D-2026-08-17's lesson — because a check is not a start.

`rxnpredict` runs with no predictor extras installed and the `fake_a`/`fake_c` deterministic
doubles requested — a real tool surface with no GPU, no checkpoint download and no model-weight
egress. See `chemclaw_mcp_rxnpredict.engine.base_doubles.register_requested`.

## Prerequisites

- `uv`, `npm`, `python3` on `PATH`.
- Docker, *or* the native fallback `infra/live/bootstrap.sh` builds (see its own header comment).
- Sibling checkouts of the three companion repos. Point at them with:
  - `CHEMCLAW_MCP_REPO` (default `/workspace/8fqycwdt8v-oss/chemclaw3-mcp`)
  - `CHEMCLAW_MOCK_REPO` (default `/workspace/8fqycwdt8v-oss/chemclaw3_mock`)
  - `CHEMCLAW_UI_REPO` (default `/workspace/8fqycwdt8v-oss/chemclaw3_ui`)
- An Anthropic credential: `ANTHROPIC_API_KEY`, or an env var literally named `API-KEY` (this
  script maps it — see `CLAUDE.md`'s "Local live/e2e credentials" note for where that comes from
  in this environment).

## Running it

```sh
make live-e2e-full-stack        # infra + all four repos + readiness polling throughout
open http://127.0.0.1:5173      # the real UI, talking to the real stack
make live-e2e-full-stack-down
```

Or drive it directly: `infra/live/e2e-full-stack/up.sh [up|down|status|restart <name>]`.
`restart <name>` (`props`, `rxnpredict`, `calc`, `mock-eln`, `mock-vendor`, or `ui-bff`) kills and
restarts one external process in place — the primitive the chaos round uses. Restarting a piece of
this repo's own stack (a connector, a worker, and `chem` or `safety`) is
`infra/live/processes.sh restart <name>` instead; asking this script for one of those two says so
rather than reporting an unknown process.

## The corpus is backfilled on bring-up, and it takes hours

The last thing `up` does is start an `ElnSyncWorkflow` from the epoch. Without it the ORD half of
the seeded data is **permanently invisible**: all ~10,000 exports share one mtime (the moment the
repo was cloned) and carry older payload timestamps, so the incremental cursor passes them on its
first firing and no later run can qualify them again. The bring-up only *starts* the drain and
waits 120 s — a PR-gate proposal costs ~1.8 s, so the 4,251 ingestible records take a little over
two hours — and the log lands in `.live/e2e-corpus-backfill.log`.

`make live-data` is where a shortfall shows up, and it names the number:

```sh
make live-data                       # ... | corpus is reachable | FAIL | 1936/4251 ... |
make live-data ARGS=--corpus-only    # the value checks alone, ~7s, no infrastructure needed
```

It also reports what can never arrive: 5,760 of the seeded ORD records (the flow-Suzuki screen)
carry a coupling partner the source paper publishes only as a shorthand, so the adapter refuses
them rather than inventing a structure. That is declared, not discovered — see
`D-2026-08-18-a-corpus-is-not-reachable-because-it-is-on-disk`.

## Checking it is really wired up

```sh
curl -s localhost:8000/readyz | python3 -m json.tool     # props, rxnpredict, mock-vendor all listed
curl -s localhost:8000/metrics | grep chemclaw_connectors_unhealthy
```

Absence of an error is not success — check one of these two, same rule
`Chemclaw3-mcp/docs/integration.md` gives for a single connector.

## Logs

Every process's stdout/stderr lands in `.live/e2e-<name>.log` (this script) or `.live/<name>.log`
(`infra/live/processes.sh`'s own processes).
