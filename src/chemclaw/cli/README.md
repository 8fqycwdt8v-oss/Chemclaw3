# `chemclaw.cli` — every terminal entrypoint

**Responsibility:** the commands a human or `make` invokes, and nothing more. Each is a thin shim
over a library entry point — argument parsing, exit codes, and printing. **No logic lives here**; if
a CLI needs a behaviour, that behaviour belongs in the package that owns it, so the web front door
and the terminal get the same answer.

| | |
| --- | --- |
| `chat.py` | the admin chat REPL, and the `chemclaw` console script (`make chat`) |
| `connectors_dev.py` | every enabled connector in one dev process (`make connectors`) |
| `schedules.py` | apply the Temporal Schedules (`make schedules-apply`); the logic is `durable/schedules.py` |
| `tenants.py` | register and list tenants (`make tenant-add TENANT=acme [DEPLOYMENT=<release>]`, `make tenant-assign`, `make tenant-list`); an operator's command, run under the owning principal |
| `validate_*.py` | the validators `make` runs — one per declaration format; `validate_kg.py --compare` also compares the notes tree with the Postgres store (`make kg-validate-compare`); `validate_template_args_live.py` is the argument check `template-validate` cannot make, taken against running servers |
| `reembed.py` | bring every stored vector to the configured embedding model and width (`make reembed`); the logic is `durable/reembed.py` |
| `backfill_corpus.py`, `backfill_publications.py`, `refresh_baseline.py`, `synthesize.py`, `sync_share.py` | one-shot operational jobs: notes from existing documents, queueing results computed before a sink existed, the eval baseline, a memory-synthesis job, crawling a mounted share |
| `rekey_campaigns.py`, `rekey_compounds.py` | carry recorded rows across an identity-derivation bump (`make rekey-compounds`) |
| `erase_actor.py`, `explain.py` | offboard a person's data (`make user-erase`), recorded in the erasure ledger, and re-apply the ledger after a restore (`make user-erase-replay`); reconstruct why a session's tool calls happened (`make explain`) |
| `openapi.py` | regenerate the published API contract `schema/api/openapi.json` (`make openapi`); the logic is `api/contract.py` |
| `sink_schema.py`, `egress_preload.py` | print the DDL a results database needs; print the egress posture `deploy/entrypoint.sh` arms the compiled guard with |
| `distill.py`, `trajectory_census.py`, `propose_profile.py` | mine stored conversations: recurring trajectories, proposals, profile candidates (`make distill`, `make trajectory-census`, `make propose-profile`) |
| `live_*.py`, `leak_probe.py`, `retrieval_arms.py`, `hypothesis_recovery.py`, `verifier_margin.py`, `soak_report.py`, `phoenix_publish.py` | the live lane and measurement drivers behind the `make live-*` targets |
| `restore_drill.py` | the backup-and-restore drill (`make restore-drill`; its SQL is `core/restore_check.py`): a throwaway Postgres and Temporal in Docker, a base backup plus archived WAL replayed to a marked instant, then row counts, ledger, indexes, grants, pgvector, `kg-validate`, an erasure replayed from the erasure ledger and Temporal checked against the primary; one JSON report on stdout (`docs/guides/runbook.md` § Backup, restore and PITR) |
| `model_text_inventory.py`, `model_text_eval.py` | the inventory of every string a model reads (`make model-text`, written to `schema/model-text/`) and the ship-or-not evaluation of a batch of edits to it (`make model-text-eval`; `docs/guides/model-text-evaluation.md`) |
| `kg_import.py` | load the notes directory into the Postgres note store, idempotently, with arrival dates and a summary that names what it skipped (`make kg-import`; the logic is `kg/importer.py`) |
| `kg_store_bench.py` | measure the Postgres note store in a scratch schema: notes per second with one and four writers, query latency and resident memory at a corpus size, as JSON (`python -m chemclaw.cli.kg_store_bench`) |
| `bench_backend_rpc.py` | time `calculation_key` over MCP and over `/rpc` against the configured calc server (`make bench-backend-rpc`) |
| `architecture_baseline.py` | measure the architecture programme's baseline — import time, build time, prose ratio, sizes — into `docs/planning/architecture-baseline-<date>.json` (`make architecture-baseline`) |
| `mock_llm.py`, `storm_behaviours.py`, `delegation_behaviours.py`, `e2e_behaviours.py` | the OpenAI-compatible loopback mock and the scripted behaviour catalogues it plays |
| `kind_stale_images.py` | which kind Deployments run a superseded image under an unchanged tag (`deploy/kind/up.sh`, run as a file on the host's `python3`, standard library only) |

## Why the validators are here and not in the packages they check

They are what catches the class of error the type checker cannot see. `connector.yaml` and
`datasource.yaml` reference code as **strings** (`module:callable`), skills and templates likewise —
`mypy --strict` cannot follow a string, so without these a stale pointer fails in a production
worker instead of in CI. Keeping them together makes the set visible: one command per declaration format, each guarding a
declaration against the live surface (the count is the Makefile's `ci` target, not this file's).

`chemclaw.cli` is the outermost layer: nothing in `src/` imports it, and its downward edges are
declared in `tests/test_layering.py` like every other package's.
