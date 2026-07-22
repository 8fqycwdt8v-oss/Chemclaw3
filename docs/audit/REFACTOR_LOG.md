# Refactor Log (Phase 10)

One line per executed backlog item: finding ID · what changed · commit · test evidence.
Full suite (`make check`, 356 tests) is re-run at each wave boundary; per-item changes are
verified with `ruff` + `mypy --strict` + the targeted test file(s) for the area.

## Wave A — low-risk fixes (no external-contract change)

| ID | Change | Commit | Test evidence |
|----|--------|--------|---------------|
| A1 · INV-1 | Declare `httpx>=0.28` as a direct dependency; relock/sync | `e7df09c` | ruff ✓, `uv lock`/`uv sync` resolved; no behavior change |
| A2 · SEC-1 | `service/runner.py` logs the exception server-side, emits a generic session-keyed `ErrorEvent` (no raw `{exc}`) | `3bed2fa` | `test_service_events.py` 3 passed; new asserts raw text absent + session id present |
| A3 · COR-5/CON-2 | `mcp_servers/fpstore.py` forwards `pg_statement_timeout_seconds` to `db.connect` like every sibling store | `b554c32` | `test_molfp.py` 9 passed; new offline regression pin on the timeout kwarg |
| A4 · COR-1 | **DROPPED — verified false positive** (JSONDecodeError is already retryable under Temporal exact-name matching) | `7466ccb` (doc) | Verified vs `retry_logic.rs` + `_failure_converter.py`; no code change |
| A5 · CON-1 | Unify the `"unknown"` actor sentinel on `settings.service_actor_id` (models, agent, audit middleware) | `d2ca552` | `test_audit.py`/`test_qm_tools.py` 9 passed, 2 skip |
| A6 · CON-3 | Extract one shared `eln.adapter.parse_iso_utc`; both adapters call it | `25d2c53` | `test_eln.py` incl. new helper test; 46 passed |
| A7 · CON-4 | Rename the two same-named `_list` helpers → `_require_list` / `_optional_list` | `c84016b` | `test_eln*.py` 46 passed (pure rename) |
| A8 · CON-5 | `agents/identity/hpc_bridge.py` uses `getLogger(__name__)`; test updated | `39f2321` | `test_hpc_bridge.py` 4 passed |
| A9 · SEC-6 | One shared `chemclaw.http.error_detail` bounds upstream bodies; 5 sites routed through it | `acb8403` | `test_http.py` + nextflow/workload/obo suites, 15 passed |
| A10 · SEC-7 | Generic 401 detail; log the validation reason server-side | `a2a048d` | `test_auth.py` incl. new leak assertion, passed |
| A11 · SEC-5 | Config-gated security-headers middleware (CSP/nosniff/frame-deny/HSTS) on the front door | `aa8eeeb` | `test_service.py` 7 passed; UI still loads under CSP |

**Wave A boundary:** full `make check` — see the commit that follows for the recorded result.
