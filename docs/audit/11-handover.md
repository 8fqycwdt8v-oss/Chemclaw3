# 09 — Handover (Phase 11)

Execution of the approved backlog is complete. This is the before/after and what future
contributors should know. Per-item detail is in `REFACTOR_LOG.md`; findings and rationale in
`00-SUMMARY.md`.

## Baseline diff (before → after)

| Gate | Before (audit start) | After | Notes |
|---|---|---|---|
| `ruff check` + `format` | clean (196 files) | clean (198 files) | +`chemclaw/http.py`, +tests |
| `mypy --strict` | clean (186 files) | clean (189 files) | +`chemclaw/http.py` and new tests |
| `pytest` | 356 passed / 25 skipped / **0 failed** | 369 passed / 27 skipped / **0 failed** | +13 tests; the 2 extra skips are new Postgres-backed tests (run in CI) |
| `pip-audit` | no known CVEs | no known CVEs | `httpx` now declared (was transitive) — no new CVEs |
| `kg-validate` / `eln-validate` / `skill-validate` | pass | pass | unchanged |
| `eval` | exits 0 (report only) | exits 0 (report only) | `evals/` untouched; the "gated metric" lines are pre-existing case-set reporting |
| Secrets (history) | none | none | unchanged |

Net: **+13 regression/characterization tests, zero failures, all gates still green**, plus the
structural fixes below. No dependency CVEs introduced.

## What changed (by finding)

**Security**
- **SEC-1** — turn errors no longer stream raw exception text to the browser; a generic
  session-keyed message goes out, the detail is logged server-side.
- **SEC-2** — a loud startup warning fires when `entra_required=False` and the bind is non-loopback.
- **SEC-3** — audit-sink failures log at ERROR with a stable `audit_sink_failure` marker (alertable).
- **SEC-4** — the chat message is size-bounded (→422) and `expand_note(hops)` is clamped.
- **SEC-5** — the front door sets CSP / nosniff / frame-deny / HSTS (config-gated, default on).
- **SEC-6** — upstream HTTP error bodies are bounded before landing in exceptions/logs.
- **SEC-7** — token-validation failures return a generic 401; the reason is logged, not disclosed.

**Correctness / resilience**
- **COR-2** — the Nextflow launch carries a deterministic `Idempotency-Key` (no double-submit on retry).
- **COR-3** — the front-door live-session map is a bounded LRU (no per-pod memory growth).
- **COR-4** — push-back events are claimed atomically (`FOR UPDATE SKIP LOCKED`); no concurrent double-delivery.
- **COR-5** — the fingerprint store applies the per-statement timeout like every other store.
- **COR-1** — **not a bug** (verified false positive; JSONDecodeError is already retryable under Temporal's exact-name matching). No change.

**Consistency / duplication / architecture**
- **CON-1** — the `"unknown"` actor sentinel is unified on `settings.service_actor_id`.
- **CON-3 / CON-4** — one shared `parse_iso_utc`; the two `_list` helpers renamed to their contracts.
- **CON-5** — the HPC bridge uses `getLogger(__name__)`.
- **DUP-1 (+DUP-2/3)** — one source registry; memory synthesis honors `data_sources`; `eln/registry.py` deleted (ADR D-053).
- **ARC-1** — the wheel packages all 15 first-party modules (a non-editable install works).
- **INV-1** — `httpx` is a declared direct dependency.
- **INV-3** — the durable audit sink has a direct (CI) test.

## Behavior / contract changes (call out to operators)

1. **Memory synthesis now honors `CHEMCLAW_DATA_SOURCES`** (ADR D-053). With the default
   `data_sources="graph,eln-json"`, memory reads the JSON ELN source only — **add `eln-ord` to the
   config to include ORD reactions in memory synthesis.** The durable sync and memory jobs now read
   the identical source set.
2. **Oversized chat messages get a 422** past `CHEMCLAW_SERVICE_MAX_MESSAGE_CHARS` (default 100k).
3. **The wheel build contract changed** — a non-editable `pip install` now ships all packages (it
   previously shipped only `chemclaw/` and a broken console script). Editable installs are unaffected.
4. **Push-back delivery is now at-most-once** (was at-least-once) in the tiny crash window between
   claim and delivery — the trade for eliminating concurrent double-delivery (COR-4). Acceptable for
   a wake notification whose durable result lives elsewhere.
5. New config fields (all defaulted, documented in `.env.example`): `service_security_headers`,
   `service_max_message_chars`, `service_max_live_sessions`, `graph_max_hops`.

## Conventions established (so the drift doesn't recur)

- **One config surface.** Every URL/threshold/timeout/limit is a `Settings` field, ENV-overridable,
  documented in `.env.example`. No `Field(max_length=<settings>)` frozen-at-import — read config in a
  validator when it must be runtime-adjustable.
- **One source registry** (`sources/registry.py`), config-driven. Do not reintroduce a parallel
  hardcoded adapter list; add a source as one registry entry + one `data_sources` token.
- **Every Postgres store passes `statement_timeout_seconds`** to `chemclaw.db.connect`.
- **Client-facing errors are generic; detail is logged server-side** (SEC-1/SEC-7). Never interpolate
  an exception or an upstream body into a client response.
- **Unattributed identity is `settings.service_actor_id`**, never a `"unknown"` literal.
- **Shared HTTP-error formatting** goes through `chemclaw.http.error_detail` (bounded).
- **New durable/Postgres or Temporal code needs a CI-run test** (`tests/pg.py` / `tests/temporal_env.py`),
  since the offline sandbox skips those paths.

## What's left (deferred, unchanged)

- The **informational** findings were intentionally not changed: INV-2 (the harmless SQL-005
  numbering gap), SEC-8 (audit logs user text + oid — a documented GxP requirement), SEC-9 (dev-default
  DSN), DUP-4 (`exchange_obo` dormant by design). See `00-SUMMARY.md`.
- The **live-infrastructure edges** remain as `DEFERRED.md`/`BACKLOG.md` describe (real Entra tenant,
  Temporal broker, OpenShift cluster). The COR-2 idempotency and COR-4 concurrency fixes are validated
  in CI (Postgres) and by unit tests, but their real-cluster behavior is exercised only once those
  edges are wired.
- The **agent-harness double-integration** watch item surfaced no residual dead code while working
  nearby; no deletion was needed. If a second harness path is later found, confirm supersession first.

## Follow-up pass (2026-07-22, post-merge)

After PR #9 merged, a second pass closed the remaining open points and critically re-reviewed the
deferrals:

- **ELN multi-ingest cursor guard** — `sync_eln_entries` now fails fast + non-retryably when more
  than one ingest source is active, since the sync tracks one shared high-water cursor (a second
  source would silently skip the lagging one). This is the interim validator `DEFERRED.md` names;
  the full per-source-cursor fix stays deferred until a second real feed lands.
- **CI coverage gate** — `[tool.coverage.report] fail_under = 80` and CI runs `make lint type cov`.
  The floor sits safely below the measured offline baseline (86%, with Postgres/Temporal skipped; CI
  runs those and is higher). Ratchet upward over time.
- **Informational closures** — SEC-8 (audit-trail PII/retention) documented in `SECURITY.md`; INV-2
  (the SQL `005` numbering gap) documented in `infra/sql/006_audit_events.sql`; the harness
  double-integration question (Open Q #5) resolved as a false alarm (two different "harness" features).
- **Critical deferred-items re-review** — every `DEFERRED.md` item re-checked against current
  reality; all remain correctly deferred except the cursor guard above. See the "Critical re-review"
  section in `DEFERRED.md`.
- **Doc cleanup** — the eight interim per-phase audit reports were consolidated into `00-SUMMARY.md`
  and removed (detail preserved in git history); the durable audit record is now `00-SUMMARY.md`,
  `REFACTOR_LOG.md`, and this file.

## Follow-up pass 2 (2026-07-22, "close all found gaps")

A third pass closed the two deferrals that a re-check showed were implementable offline against the
*existing* contracts (no live infra, no speculative abstraction) — see **ADR D-054**:

- **Per-source ELN cursors** — the durable sync now iterates every active ingest source and keys
  one high-water cursor per source (the `sync_cursors` table already keyed by name). The interim
  fail-fast guard from follow-up pass 1 is removed; multi-ingest is first-class. This is a faithful
  generalization of today's `ElnAdapter` contract (both adapters are datetime-cursored), not a guess
  about the not-yet-built Snowflake source. The now-dead `eln_sync_adapter` config field was removed
  (audit **DUP-2**), and `.env.example` + runbook (iii) were corrected to the `data_sources` reality.
- **Per-scope token lock** — `WorkloadTokenProvider` collapses concurrent cold-cache callers onto a
  single federation exchange via a per-scope `asyncio.Lock` + double-checked cache re-read.

**Contract notes (dev-stage, no live cluster):** the sync's stored-cursor keying moved from one
label to per-source names (first scheduled run re-ingests each source from epoch once — idempotent);
removing `eln_sync_adapter` means a deployment that set `CHEMCLAW_ELN_SYNC_ADAPTER` must drop it
(`extra="forbid"`). Both are safe now because the F-layer live edges are still open.

**What remains deferred is now genuinely infra/scale/scope-gated** (real Entra tenant / Temporal
broker / OpenShift cluster / Snowflake feed, a scale not yet reached, or a product-scope decision) —
implementing those offline would be untested boilerplate, which the repo's quality rules forbid. See
`DEFERRED.md` and `BACKLOG.md` for the itemized reasons.
