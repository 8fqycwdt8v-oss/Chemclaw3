# D-066 — Resilience hardening: DB-query clamps, session reattach, turn/token budgets

**Context.** A review against four failure modes seen in another agent system (no memory on restart,
no idempotency, no budget, unbounded DB queries) found Chemclaw already covers idempotency (D-011
content-addressed cache + workflow-id dedup) and durable job execution (Temporal), but left three
concrete residual gaps: (1) an agent-supplied `top_k` and the substructure scan could issue an
unbounded query/full-table load — the closest analog to "no row limits"; (2) the front door held
live sessions only in an in-process LRU, so a pod restart forced returning clients onto new sessions,
orphaning their durable history + unconsumed push-back; (3) a single turn is iteration-capped but
nothing capped the *number* of turns, leaving cumulative LLM spend unbounded (the "$400 loop").

**Decision.** Closed all three on the feature branch, each config-gated to preserve today's behavior:
- **DB clamps (#4).** `find_matches` clamps a model-supplied `top_k` to `[1, fingerprint_max_top_k]`
  (default 100) — the fingerprint-search analog of the existing `graph_max_hops` clamp, applied at
  the one DRY chokepoint both similarity entry points share. `all_records(limit)` gained a bounded,
  id-ordered variant; `find_substructure_matches` scans at most `substructure_scan_max_records`
  (default 5000) and **logs a warning when it truncates** (no silent cap). The universal 30s
  `statement_timeout` remains the time backstop; these bound rows materialized into the worker heap.
- **Session reattach (#1).** New `session_owners` table (migration 013) + `SessionOwnerStore` record
  one durable identity row per session at creation. On a live-cache miss the front door looks the
  owner up, authorizes the caller, and rebuilds the live handle over the same durable history
  (`PostgresHistoryProvider` reloads the thread on first use). Gated on `session_store="postgres"`
  (rehydration is meaningful only with durable history); under the in-memory store a miss stays a
  404 (unchanged). Owner-scoped: a different user still gets a 404, no existence leak. This is the
  *front-door restart-reattach* gap, distinct from the still-open mid-flight same-turn resume
  (BACKLOG "resuming the same streamed turn mid-flight", D-032/D-035 seam).
- **Turn/token budgets (#3).** New `service.budget.BudgetTracker` meters each turn's reported token
  usage (`UsageDetails` from MAF's usage content) and counts turns per session and per user; the
  front door refuses a turn (HTTP 429) that would exceed a cap, before taking an admission permit.
  Five config knobs (`budget_enabled` + per-session/per-user turn+token caps, 0 = unlimited), off by
  default. In-process and best-effort — the missing ceiling above the per-turn loop cap; it partly
  closes the AG-15 "per-user quota" deferral (in-process now; durable rolling-window still deferred).

**Consequence.** The three residual gaps are closed for a running deployment. Two conscious
deferrals recorded in DEFERRED.md: a durable rolling-window budget quota (survives restart /
multi-pod), and the substructure pattern-fingerprint prefilter (sound screening at ~10⁴+ molecules).

**Result.** `make lint type test` green (490 passed, 34 infra-skipped). New tests: `test_budget.py`
(tracker caps + usage metering), `test_service.py` (rehydration, owner-scoping, 429 over budget),
`test_molfp.py` (top_k clamp, bounded scan + truncation warning), `test_session_store.py`
(`SessionOwnerStore` round-trip), `test_config.py` unaffected.
