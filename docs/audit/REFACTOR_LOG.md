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

**Wave A boundary:** full `make check` green — 360 passed, 25 skipped, 0 failed (`6fbc2ce`).

## Wave B — structural & resilience (contract/behavior-affecting)

| ID | Change | Commit | Test evidence |
|----|--------|--------|---------------|
| B1 · DUP-1 (+DUP-2/3) | Memory synthesis reads `active_ingest_sources()` (honors `data_sources`); delete `eln/registry.py`; clarify `eln_sync_adapter` as cursor label; ADR D-053 | `a539356` | New `test_memory_jobs.py` (RED→GREEN) pins config-driven corpus; `test_datasource_seam`/`test_eln` green |
| B2 · ARC-1 | Wheel declares all 15 first-party packages; align coverage source list | `7145d5d` | Built wheel ships all packages; `chemclaw` console script resolves on a non-editable install |
| B3 · SEC-2 | Loud startup warning when `entra_required` false + non-loopback bind | `88db128` | Parametrized matrix test (exposed/loopback × auth on/off) |
| B4 · COR-3 | Bound the front-door live-session map with an LRU (`_LiveSessions`, config cap) | `5bf0c68` | Eviction + capacity tests; owner-scoping integration test still green |
| B5 · COR-4 | Atomic `claim_unconsumed` (`UPDATE … FOR UPDATE SKIP LOCKED … RETURNING`) | `46d972e` | Tailer unit test; new CI concurrency test (two claimers partition rows) |
| B6 · COR-2 | Deterministic `Idempotency-Key = qm_job_key(job)` on the Nextflow launch | `d301c39` | Test asserts key == job key, stable across retry |
| B7 · SEC-4 | Config-driven `message` max via field_validator (→422); clamp `expand_note(hops)` | `3b9c322` | Oversized-message 422 test; hops-clamp test |

**Wave B boundary:** full `make check` green — 369 passed, 26 skipped, 0 failed.

## Wave C — coverage & design-choice items

| ID | Change | Commit | Test evidence |
|----|--------|--------|---------------|
| C1 · INV-3 | Postgres round-trip test for `PostgresAuditSink` (the GxP sink) | `900a6d3` | New `test_audit_store.py` (CI-run; skips offline) |
| C2 · SEC-3 | Audit-sink failure logged at ERROR with stable `audit_sink_failure` marker + trail ids | `afe00cb` | Updated sink-failure test asserts ERROR level + structured marker |

**Final gate:** see `11-handover.md` for the before/after baseline diff.
