# `tests/` — the suite, and several gates that are not about behaviour

Run it with `make test`, one file with `pytest tests/test_x.py::test_name`, one pattern with
`pytest -k "substring"`. `make check` adds lint and `mypy --strict`; `make cov` adds the 80% floor.
CI runs exactly these targets, so a green `make` locally is a green CI.

`conftest.py` holds the shared fixtures, `pg.py` and `temporal_env.py` the optional Postgres and
Temporal harnesses (their tests skip when the service is absent — that is why a local run reports
skips and CI does not), `fixtures/` the sample data.

## What is checked here that is not a behaviour

A handful of these modules exist to catch **structural** drift — things that are true of the
repository rather than of a function, and that no type checker can see:

| Module | Guards |
| --- | --- |
| `test_layering.py` | the four layers: `core` imports no sibling, retrieval imports no orchestration |
| `test_packaging.py` | `src/` holds exactly one package; the wheel, coverage and `mypy` agree |
| `test_repo_map.py` | every directory has a README, and `ARCHITECTURE.md` matches the tree |
| `test_deploy_chart.py` | the Containerfile COPY set, and chart ↔ entrypoint in **both** directions |
| `test_decision_log.py` | ADR ids are unique and the ledger matches the files |
| `test_no_egress.py` | no shipped module names a third-party data host (D-089) |

## A structural test must be shown failing

These have a specific failure mode: they break by **finding nothing** rather than by raising. An
empty `glob` and an empty discovery loop both read as success, so a test that iterates a now-empty
set reports green while asserting nothing. It has happened twice —
`test_image_ships_every_first_party_package` went vacuous when D-148 left no root packages to
discover, and `make db-migrate` globbed a moved directory and applied zero migrations in silence.

So: after writing or moving one of these, **break it on purpose and watch it fail** before trusting
the green. Each of the modules above now asserts against a non-empty set explicitly, which is the
cheap version of the same discipline.
