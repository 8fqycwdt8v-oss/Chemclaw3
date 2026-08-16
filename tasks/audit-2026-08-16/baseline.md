# Fresh-eyes audit — Phase 0 baseline

Measured 2026-08-16 with the infrastructure **live** (Docker daemon started, Postgres/pgvector and
Temporal up via `make up`, all 46 migrations applied, `.venv` synced from `uv.lock`). Every number
below is a measurement, not a claim carried over from a document.

## Environment as found

| | |
|---|---|
| Docker daemon | installed, **not running** at session start — started with `sudo -n dockerd &` |
| `.venv` | absent — created with `uv sync` |
| `helm` / `kubeconform` | absent — installed (v3.13.0 / v0.6.7) so `make helm-validate` runs locally |
| Postgres + Temporal | up; 46 migrations applied cleanly |

This matters: on the environment as found, a full `pytest` skips every Postgres- and Temporal-backed
test and still prints a green line.

## Repository scale (all four repos)

| Repo | Production code | Tests |
|---|---|---|
| `Chemclaw3` | 341 `.py`, **72,006 LOC** across 13 packages | 231 `test_*.py`, 3,073 test functions, 78,472 LOC |
| `Chemclaw3-mcp` | 139 `.py`, **20,897 LOC** — 5 servers + `mcp_server_kit` | included in the above count |
| `Chemclaw3_ui` | 117 `.ts`/`.tsx`, **19,246 LOC** — React SPA + Node BFF | Playwright e2e |
| `Chemclaw3_mock` | 22 `.py`, **2,222 LOC** | `tests/` |

Commits at audit start: `Chemclaw3` e5be6d7 · `Chemclaw3-mcp` 9217011 · `Chemclaw3_mock` 2f09174 ·
`Chemclaw3_ui` 1a1f6f0.

## Gate result

| Step | Result |
|---|---|
| `make lint` | **green** — `ruff check` clean, 593 files already formatted |
| `make type` | **green** — `mypy --strict`, no issues in 593 source files |
| `make cov` | see `baseline-gate.log` (run with Postgres live) |

## Objective structure metrics

Produced with `radon`, `vulture` and an AST import-graph walk — numbers, not impressions.

### Cyclomatic complexity, D and above

| Grade | Symbol |
|---|---|
| **F (58)** | `cli/live_probes.py:61 _summary` |
| **E (35)** | `api/runner.py:112 run_turn` |
| **D (28)** | `evals/live.py:452 run_turn` |
| **D (21)** | `api/graph_stream.py:225 _from_update` |

38 further blocks sit at grade C, led by `ingest/documents/binding.py:DocumentShareBinding._is_coherent`
(20), `connectors/calc/compose.py:reaction_energy` (20), `memory/…:mine_corpus` (20) and
`api/runner_trace.py:ToolCallTrace.feed` (19).

`run_turn` at E(35) is 483 of the 774 lines of `api/runner.py`, and that module has the highest
fan-out in the tree — the two worst structural signals land on the same symbol.

### Import graph

Fan-in (how many modules import it):

| | |
|---|---|
| **160** | `core.config` — ~47% of all modules |
| 43 | `core.errors` |
| 34 | `kg.note` |
| 25 | `durable.registry` |
| 23 | `core.ids`, `core` |
| 22 | `core.metrics_bridge` |
| 21 | `connectors.registry` |
| 19 | `core.identity_context` |
| 12 | `agent.authz` |

Fan-out (how many first-party modules it imports): `api.runner` **31**, `api.app` 21,
`core.config` **20**, `agent.langgraph_agent` 17.

`core.config` having fan-in 160 *and* fan-out 20 is the notable shape: the most-depended-on module
in the tree is not a leaf.

Package-level edges are mostly downward (`agent→core` 87, `connectors→core` 53, `api→core` 46,
`science→core` 32), with `api→agent` 36, `connectors→science` 40 and `connectors→durable` 17 as
the significant lateral flows. `durable→agent` (10) and `agent→kg` (8) are the edges to examine.

### Dead-code candidates

`vulture --min-confidence 60` reports 378 lines. These are **candidates only** — dynamic
registration (Temporal workflow discovery, MCP tool registration, pydantic unions) hides real uses,
and the design lens must clear each one before anything is deleted.

## Findings already established in Phase 0

These were derived mechanically, before any reviewer opinion.

### F0-1 — Two pairs of migration files share a migration number

`infra/sql/` contains `037_bo_suggestion_provenance.sql` **and** `037_document_index.sql`, plus
`043_session_listing.sql` **and** `043_session_message_shape.sql`.

`core/migrate.py:_read_sql_files` reads with `sorted(sql_dir.glob("*.sql"))` and `migrate` applies
in `sorted(sources)` order, so ordering within a colliding pair is decided by **alphabetising the
slug** — `bo_suggestion_provenance` before `document_index`, `session_listing` before
`session_message_shape`.

Today's order is deterministic and the schema applies cleanly, so this is latent rather than live.
What makes it a finding is that the number no longer identifies a migration and **nothing guards
it**: two branches each adding an `044_` conflict on no filename, merge cleanly, and their relative
order is then settled silently by their slugs. If one of that pair ever depends on the other, the
dependency is decided by alphabet. `tests/test_schema_inventory.py` checks the README inventory
against the migrations but asserts nothing about number uniqueness.

Severity: **medium** (latent, no current misbehaviour). Fix: a test asserting migration numbers are
unique, and renumber one file of each pair — safe only for files whose statements have already been
applied identically everywhere, which the existing drift guard can confirm.

### F0-2 — Documented numbers that disagree with the code

- `tests/README.md` and the comment in `.github/workflows/ci.yml` both state an **80%** coverage
  floor. `pyproject.toml` sets `fail_under = 84`.
- `tests/pg.py` states "~157 tests never executed" offline. The real figure is ≤323 test functions
  across 28 files (106 `migrated_db_or_skip()` call sites).

Severity: **low** individually. Recorded because it is the pattern this audit exists to catch —
three live documents asserting a fact that a `grep` disproves.

## What this baseline is for

Every subsequent claim in this audit is measured against these numbers. A "green run" that skipped
the Postgres and Temporal lanes is not green, and any report of one must say what it skipped.
