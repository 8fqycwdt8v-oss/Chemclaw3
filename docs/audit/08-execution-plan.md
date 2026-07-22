# 08 — Refactor Execution Plan (Phase 9)

Ordered backlog derived from `00-SUMMARY.md`. **Awaiting approval of this backlog before any code
change (Phase 10).** Each item lists: the change, the test that proves it safe, and rollback.

## Sign-off decisions folded in

- **DUP-1 → honor `data_sources`.** Memory synthesis will read only the active configured sources
  (not the hardcoded union). This is an **intentional behavior change**: with the default
  `data_sources="graph,eln-json"`, ORD reactions drop out of memory synthesis until `eln-ord` is
  added to the config. Documented in the commit + `DECISIONS.md`.
- **SEC-2 → loud warning only.** The service logs a prominent startup warning when `entra_required`
  is false and bound to a non-loopback interface; it still boots.

## Ordering principle

No Critical/High exists, so ordering is: **(A)** low-risk, no-contract-change fixes first — they
stabilize shared code and are individually revertible; **(B)** the structural/contract-affecting
changes (registry consolidation, packaging, resilience) once A is green; **(C)** test/coverage and
design-choice items last. Duplicate consolidation (DUP-1) precedes any further work on the surviving
registry. Every step is its own atomic commit; `make lint type test` must be green before the next;
each is logged to `REFACTOR_LOG.md` with the finding ID, commit hash, and test evidence.

Guardrail: items touching Postgres/Temporal paths (`fpstore`, `session_events`, `audit_store`,
`memory_jobs`, `nextflow`) are validated in CI, since the offline sandbox skips those 25 tests. Where
a path has no direct test, a characterization test is written **first**.

---

## Wave A — low-risk fixes (no external-contract change)

### A1 · INV-1 — declare `httpx`
- **Change:** add `httpx>=<resolved-version>` to `[project.dependencies]` in `pyproject.toml`; keep `uv.lock` in sync (`uv lock`).
- **Test:** `make lint type test` unchanged-green; `uv sync` resolves. No behavior change (already imported).
- **Rollback:** revert the one-line manifest change.
- **Risk:** additive manifest change only.

### A2 · SEC-1 — stop leaking `{exc}` to the browser
- **Change:** `service/runner.py` — replace `ErrorEvent(message=f"...: {exc}")` with a generic message + a correlation id; `logger.exception(...)` the detail server-side.
- **Test (new, characterization-first):** extend `service/` tests to assert the SSE `ErrorEvent` payload contains the generic text and **not** the raw exception string, when a tool raises. (Existing runner tests confirm the happy path is unchanged.)
- **Rollback:** revert the handler; test reverts with it.
- **Risk:** low; the code already promised this behavior.

### A3 · COR-5 / CON-2 — fpstore per-statement timeout
- **Change:** `mcp_servers/fpstore.py:187` — `db.connect(self._dsn, statement_timeout_seconds=settings.pg_statement_timeout_seconds)` (matches every sibling store).
- **Test:** existing `mcp_servers` fpstore tests stay green (offline path unaffected); CI Postgres path exercises the connection. Add an assertion that `connect` is called with the timeout kwarg (mock) so the regression is pinned offline.
- **Rollback:** revert the one-line change.
- **Risk:** low; strictly adds a bound.

### A4 · COR-1 — Nextflow JSON-decode retryability — **DROPPED (verified false positive)**
During execution this was verified against the Temporal SDK + Rust core: `non_retryable_error_types`
is matched by **exact case-insensitive string equality** on the failure `type`
(`sdk-core/.../retry_logic.rs:104`), and a generic exception's type is
`exception.__class__.__name__` (`temporalio/converter/_failure_converter.py:113`). So Temporal sees
`"JSONDecodeError"`, not `"ValueError"` → the error is **already retryable**, which is the desired
behavior. `publish.py`'s own comment documents this exact-name behavior (it's why `"ValidationError"`
is listed explicitly). No bug, no change. See `05-correctness.md §1.1` for the full correction.

### A5 · CON-1 — unify the `"unknown"` actor sentinel
- **Change:** replace literal `"unknown"` defaults with `settings.service_actor_id` in `workflows/models.py:35`, `agents/chemclaw_agent.py:89`, `agents/audit.py:166` (and fix the two docstring mentions). Note: `workflows/models.py` is inside a Temporal workflow import closure — read the config value at the activity/caller boundary, not inside a `@workflow.defn`, to preserve determinism (verify against the Phase-7 determinism rule).
- **Test:** existing audit/agent/workflow tests green; add an assertion that an unattributed event carries `service_actor_id`, not `"unknown"`.
- **Rollback:** revert per-file.
- **Risk:** low–medium (touches the workflow model default — determinism check required).

### A6 · CON-3 — dedupe the ISO-timestamp parse
- **Change:** extract the shared `datetime.fromisoformat(v.replace("Z","+00:00"))` + naive→UTC logic (from `eln/json_adapter.py:266` and `eln/ord_adapter.py:332`) into one helper (e.g. `eln/`-level or `chemclaw/`-level util); both adapters call it.
- **Test:** existing `test_eln*` cover both adapters' timestamp handling; add a direct unit test for the helper (Z-suffix, offset, naive → all become tz-aware UTC).
- **Rollback:** inline the helper back.
- **Risk:** low; behavior identical by construction.

### A7 · CON-4 — disambiguate the two `_list()` helpers
- **Change:** rename `eln/json_adapter.py:212` `_list` → `_require_list` (raises on missing) and `eln/ord_adapter.py:371` `_list` → `_optional_list` (returns `[]`). Pure rename within each module.
- **Test:** existing adapter tests green (behavior unchanged).
- **Rollback:** revert renames.
- **Risk:** trivial.

### A8 · CON-5 — logger name + cursor idiom
- **Change:** `agents/identity/hpc_bridge.py:15` → `logging.getLogger(__name__)`. (Cursor-idiom normalization is cosmetic and deferred unless it lands cleanly in a touched file — noted, not forced.)
- **Test:** existing tests green.
- **Rollback:** revert.
- **Risk:** trivial.

### A9 · SEC-6 — truncate upstream response bodies in errors
- **Change:** in `nextflow.py`, `identity/workload.py`, `identity/obo.py`, cap the `response.text` interpolation (e.g. first N chars) or drop it in favor of status + reason.
- **Test:** existing identity/nextflow tests green; assert the error message is bounded.
- **Rollback:** revert.
- **Risk:** low; server-side only.

### A10 · SEC-7 — generic 401 detail
- **Change:** `service/auth.py` — return a generic `"invalid token"` 401 detail in production; keep the specific reason in server logs.
- **Test:** existing auth tests; assert the 401 body no longer contains the underlying validation reason.
- **Rollback:** revert.
- **Risk:** low.

### A11 · SEC-5 — HTTP security headers
- **Change:** `service/app.py` — add a small middleware setting `Content-Security-Policy` (scoped to the self-served chat UI), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and `Strict-Transport-Security` (behind a config toggle, default on in non-dev).
- **Test (new):** assert the headers are present on a sample response; assert the chat UI still loads (CSP not over-tight).
- **Rollback:** remove the middleware.
- **Risk:** low–medium — a too-strict CSP could break the inline UI; validated by the UI-load test.

---

## Wave B — structural & resilience (contract/behavior-affecting — covered by this backlog's approval)

### B1 · DUP-1 (+DUP-2, DUP-3) — consolidate onto one registry, memory honors `data_sources`
- **Change:**
  1. Repoint `workflows/memory_jobs.py` `_all_reactions()` from `eln.registry.all_eln_adapters()` to `sources.registry.active_ingest_sources()` (the ingest halves), so memory honors `data_sources`.
  2. Delete `eln/registry.py` (`ELN_ADAPTERS`, `make_eln_adapter`, `all_eln_adapters`) — no remaining production caller after (1). Migrate any test that imports it.
  3. `settings.eln_sync_adapter` is already vestigial (cursor label only). **Decision needed at approval:** remove it and key the cursor on a stable constant, or keep it purely as the cursor label with a corrected docstring. Default recommendation: keep as an explicit cursor-key label, fix the docstring (smaller blast radius; no config-contract removal).
- **Behavior change:** default `data_sources="graph,eln-json"` ⇒ memory synthesis no longer includes ORD reactions until `eln-ord` is added. Called out in the commit + a new ADR (`DECISIONS.md`).
- **Test (characterization-first):** before the change, add a test pinning today's memory-source set; after, update it to assert memory reads exactly `active_ingest_sources()` and that setting `CHEMCLAW_DATA_SOURCES` changes the memory corpus. Existing `test_memory*` and ELN-sync tests green.
- **Rollback:** restore `eln/registry.py` and the old import (single revert of the consolidation commit).
- **Risk:** medium — behavior change + deletion; mitigated by characterization tests and single-commit revertibility. This is the largest structural item; it goes before any other memory/ELN work.

### B2 · ARC-1 — wheel packaging
- **Change:** `pyproject.toml` `[tool.hatch.build.targets.wheel]` — declare all 13 first-party packages (mirror the `[tool.coverage.run] source` list), **or** record the flat multi-package layout in an ADR and configure the build accordingly.
- **Test:** `uv build` produces a wheel; in a throwaway venv, `pip install <wheel>` then `python -c "import agents, bo, calc, ...; from agents.cli import main"` and the `chemclaw` console script resolves. (Build-contract change → validated by an actual non-editable install.)
- **Rollback:** revert the build-target change.
- **Risk:** medium — build/distribution contract; no runtime code change, and current editable installs are unaffected.

### B3 · SEC-2 — startup warning on unauthenticated non-loopback bind
- **Change:** at service startup (`service/app.py` `create_app`, or config), when `entra_required=False` **and** `service_host` is non-loopback, emit a prominent `logger.warning` (single, unmissable line). Still boots (per decision).
- **Test (new):** assert the warning fires for `(False, "0.0.0.0")` and does **not** for `(False, "127.0.0.1")` or `(True, *)`.
- **Rollback:** remove the check.
- **Risk:** low; log-only, no behavior gate.

### B4 · COR-3 — bound the front-door session maps
- **Change:** `service/app.py` — replace the unbounded `sessions`/`session_owners` dicts with a bounded structure (LRU/TTL, size from config), or delegate lookup to the session store. Keep ownership semantics identical.
- **Test:** existing session tests green; add a test that inserting beyond the bound evicts oldest and does not leak, and that ownership checks still hold.
- **Rollback:** revert to the plain dicts.
- **Risk:** medium — touches session lifecycle; ownership-preservation test guards it.

### B5 · COR-4 — atomic push-back mailbox claim
- **Change:** `agents/session_events.py` — make the read-then-mark atomic with `FOR UPDATE SKIP LOCKED` (claim-then-read), so concurrent tailers can't double-deliver.
- **Test:** CI Postgres path; add a concurrency test (two tailers, assert each event delivered once). Offline: assert the query text includes the locking clause (regression pin).
- **Rollback:** revert the query change (no schema change involved).
- **Risk:** medium — DB query semantics; validated in CI.

### B6 · COR-2 — Nextflow launch idempotency  *(decision at approval)*
- **Recommended:** fix now (cheap, defensive) — send an idempotency key derived from the QM cache key on `launch_run`, so a lost-response retry doesn't double-submit. **Alternative:** defer with the rest of the HPC live edge (`DEFERRED.md`), since the path is dormant (`hpc_launch_interface` defaults `mock`) and the real Seqera contract isn't wired.
- **Change (if now):** add the idempotency header/param to the launch POST; document the key derivation.
- **Test (new):** fake transport asserts the idempotency key is present and stable for the same job input.
- **Rollback:** revert.
- **Risk:** low, but interacts with a not-yet-real upstream contract — hence the defer option.

### B7 · SEC-4 — input bounds at the trust boundary
- **Change:** `service/app.py` `MessageIn.message` → `Field(max_length=…)` (from config); clamp `agents/graph_tools.py` `expand_note(hops)` to a config max; clamp MCP `top_k`.
- **Contract note:** previously-accepted oversized bodies now return 422 — a request-contract tightening. Threshold set generously.
- **Test (new):** oversized message → 422; in-bound → unchanged; `hops` above max is clamped, not errored.
- **Rollback:** remove the constraints.
- **Risk:** low–medium (request-contract change); generous threshold mitigates.

---

## Wave C — coverage & design-choice items

### C1 · INV-3 — durable audit-sink test
- **Change:** add a Postgres-backed test for `agents/audit_store.py` (the GxP sink) using the CI Postgres fixture (`tests/pg.py`): record an event, read it back, assert fields.
- **Test:** the new test itself; runs in CI, skips offline.
- **Rollback:** remove the test.
- **Risk:** none (test-only).

### C2 · SEC-3 — audit-sink failure visibility  *(design choice at approval)*
- **Recommended (minimal):** keep swallow-and-continue (availability) but emit a structured, distinctly-named log record (or a counter hook) on sink failure so it is alertable, rather than a generic WARNING. **Alternative:** fail-closed on sink failure (GxP-strict) — bigger behavior change, not recommended without an explicit compliance requirement.
- **Test:** assert the distinct record/counter fires when the sink raises.
- **Rollback:** revert to the plain WARNING.
- **Risk:** low (minimal variant).

---

## No-action (recorded, intentionally not changed)

- **INV-2** (SQL 005 gap) — confirmed never existed; glob-by-filename discovery makes it harmless.
- **SEC-8** (audit logs user text + oid) — intentional, documented GxP requirement, config-bounded.
- **SEC-9** (dev-default DSN) — documented local default; all real secrets default empty.
- **DUP-4** (`exchange_obo` dormant) — intentionally wired-but-dormant, gated + tested.
- **Agent-harness double-integration** — will spot-check for residual dead paths while working nearby;
  no deletion without confirming supersession (open question #5).

---

## Verification & handover (Phase 11, after execution)

Re-run the full Phase-2 baseline and diff: test pass count, lint/type status, CVE count, plus the new
regression tests. Produce `09-handover.md` (what changed, what's deferred and why) and fold the new
conventions (one registry, `service_actor_id` not `"unknown"`, per-statement timeouts on every store,
generic client-facing errors) into `DECISIONS.md`/a CONTRIBUTING note so the drift doesn't recur.
