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

## Running on a loaded machine: `PYTEST_TIMEOUT_SCALE`

Every test is capped at 180 s (`pyproject.toml`), and a few compute-bound ones carry a tighter
`@pytest.mark.timeout(...)` so a spiking optimizer names itself instead of eating the whole file's
budget. **A marker overrides `--timeout` and `PYTEST_TIMEOUT`**, so the tightest caps were the ones
no command line could relax — the wrong way round when the machine is busy.

`PYTEST_TIMEOUT_SCALE` multiplies every cap, markers included:

```
PYTEST_TIMEOUT_SCALE=4 make test    # ~4x slack, every cap, same relative tightness
```

Reach for it when you see the `timeouts — these assertions never ran` section in the output. That
section exists because a timed-out test is **not evidence about the code**: its assertions never
ran, and reading one as a numerical failure has already cost this repository a wrong baseline that
six agents worked against for hours. `tests/test_suite_timeouts.py` pins both the scaling and that
section.

CI does not set it (the gate runs in ~5 minutes on a dedicated runner); it is for a developer
machine or a sandbox running several jobs at once.
