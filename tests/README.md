# `tests/` — the suite, and several gates that are not about behaviour

Run it with `make test`, one file with `pytest tests/test_x.py::test_name`, one pattern with
`pytest -k "substring"`. `make check` adds lint and `mypy --strict`; `make cov` adds the coverage
floor — `fail_under` in `pyproject.toml` is the one place that number lives, so it is not restated
here.
CI runs exactly these targets, so a green `make` locally is a green CI.

`conftest.py` holds the shared fixtures, `pg.py` and `temporal_env.py` the optional Postgres and
Temporal harnesses (their tests skip when the service is absent — that is why a local run reports
skips and CI does not), `fixtures/` the sample data.

Four modules hold shared **doubles**, and which one you want depends on how much of the engine you
mean to exercise. `fakes.py` is the ASGI client and the model that accepts tool binding;
`fakes_langgraph.py` is `ScriptedChatModel`, a model that replays a fixed script of tool
calls and answers, for tests that drive a compiled graph directly; `fakes_turn.py` is
`ScriptedTurn`, one turn's behaviour written once and injected through `run_turn`'s `graph_factory`
— the seam a turn is driven through, so a test never needs a model credential; `legacy_rows.py`
holds the stored-message payloads the previous framework wrote, frozen as literals because they are
historical data a production table still contains.

The other non-test modules are narrower harnesses: `middleware.py` drives one `wrap_tool_call`
middleware without compiling a graph, `signals.py` captures what code publishes on the stream
writer, `surface.py` reads what one profile advertises without building an agent, `bo_harness.py`
is the BO ask/tell loop `src/` deliberately does not ship, `calc_server_fake.py` and
`warehouse_fake.py` stand in for the calculation server and a warehouse, `document_fixtures.py`,
`parse_stalls.py` and `egress_probe.py` serve the document-parsing tests, `recorded_tool_results.py`
and `recorded_workflow_histories.py` hold recorded payloads and the Temporal replay check, and
`siblings.py` locates the companion checkouts.

## What is checked here that is not a behaviour

A handful of these modules exist to catch **structural** drift — things that are true of the
repository rather than of a function, and that no type checker can see:

| Module | Guards |
| --- | --- |
| `test_layering.py` | the four layers: `core` imports no sibling, retrieval imports no orchestration |
| `test_third_party_layering.py` | the other half: which package may import which third-party *stack*, and that nothing reaches into a dependency's private modules |
| `test_packaging.py` | `src/` holds exactly one package; the wheel, coverage and `mypy` agree |
| `test_repo_map.py` | every directory has a README, and `ARCHITECTURE.md` matches the tree |
| `test_deploy_chart.py` | the Containerfile COPY set, and chart ↔ entrypoint in **both** directions |
| `test_helm_chart.py` | the chart's configuration keys and the app's `Settings`, both directions |
| `test_decision_log.py` | ADR ids are unique and the ledger matches the files |
| `test_deferred_register.py` | `DEFERRED.md` stays a register of pending work, not a log of past reviews (D-154) |
| `test_no_egress.py` | no shipped module names a third-party data host (D-089) |
| `test_vendored_source.py` | the one sanctioned vendored dataset stays the only escalation of that rule |
| `test_migrations_are_additive.py` | no migration destroys data or ends the "deploy the previous image" rollback |
| `test_schema_inventory.py` | `infra/sql/README.md` lists exactly the tables that exist, with the migrations that touch each |
| `test_database_privileges.py` | the SQL grant matrix is derived from the code rather than maintained beside it |
| `test_metric_declarations.py` | every metric name a call site uses is declared |
| `test_docstring_paths.py` | every module path a docstring or comment points at is a file that exists |
| `test_prose_contract.py` | the agent's prose names only capability the agent actually has |
| `test_dead_vocabulary.py` | a superseded ADR's vocabulary is not used as current in the record |
| `test_claude_md_figures.py` | `CLAUDE.md`'s "today" section states no figure a symbol should hold |

## A structural test must be shown failing

These have a specific failure mode: they break by **finding nothing** rather than by raising. An
empty `glob` and an empty discovery loop both read as success, so a test that iterates a now-empty
set reports green while asserting nothing.

So: after writing or moving one of these, **break it on purpose and watch it fail** before trusting
the green. Each of the modules above pins itself against emptiness explicitly — either a count floor
(`assert len(files) >= 30`) or a both-directions check, where a declared row that stops being
observed fails as loudly as an observation with no row. That is the cheap version of the same
discipline, and adding a module to this table means giving it one.

**A count floor is not always enough, and `test_migrations_are_additive.py` is the worked example.**
On a `--depth=1` checkout its history check would compare every file against itself — a
healthy-looking count — so what it counts is comparisons that span a commit. If a check reads the environment rather than only
the tree, ask what the environment can take away without changing the count.

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
ran. `tests/test_suite_timeouts.py` pins both the scaling and that
section.

CI does not set it — a dedicated runner has the whole machine, and no committed probe measures
how long the gate takes there, so no duration is claimed here (the Actions run page is the live
figure). It is for a developer
machine or a sandbox running several jobs at once.
