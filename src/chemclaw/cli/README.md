# `chemclaw.cli` — every terminal entrypoint

**Responsibility:** the commands a human or `make` invokes, and nothing more. Each is a thin shim
over a library entry point — argument parsing, exit codes, and printing. **No logic lives here**; if
a CLI needs a behaviour, that behaviour belongs in the package that owns it, so the web front door
and the terminal get the same answer.

| | |
| --- | --- |
| `chat.py` | the admin chat REPL, and the `chemclaw` console script (`make chat`) |
| `connectors_dev.py` | every enabled connector in one dev process (`make connectors`) |
| `schedules.py` | register the Temporal schedules |
| `backfill_corpus.py`, `refresh_baseline.py` | one-shot operational jobs |
| `validate_*.py`, `verify_audit_chain.py` | the validators `make` runs |

## Why the validators are here and not in the packages they check

They are what catches the class of error the type checker cannot see. `connector.yaml` and
`datasource.yaml` reference code as **strings** (`module:callable`), skills and templates likewise —
`mypy --strict` cannot follow a string, so without these a stale pointer fails in a production
worker instead of in CI. Keeping them together makes the set visible: eight commands, one per
declaration format, each guarding a declaration against the live surface.

`chemclaw.cli` is deliberately absent from the sibling list in `tests/test_layering.py`. Nothing
imports *it* — it is the outermost layer — so a dependency rule about it would assert something no
edge can violate.
