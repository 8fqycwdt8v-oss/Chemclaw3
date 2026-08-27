# Agent engine deep audit → implementation pass (2026-08-27)

A four-agent investigation of the agent engine (tool-call path, long-turn state, durable delivery,
decision record) produced 14 defects, ~20 risks and 12 opportunities; this pass implements the
findings. Every declined/reverted approach in the decision record was honored — nothing here
re-litigates stream_events v3, `ModelCallLimitMiddleware`, `RubricMiddleware`, summarization, the
retry middlewares, LangSmith or the harness profile.

## Plan (all done unless marked)

- [x] Baseline: Docker up, `make up`, `db-migrate`, full suite green with Postgres (4891 passed).
- [x] **A — config/validators**: `agent_max_parallel_tool_calls` → `max_concurrency` (measured:
      8-call batch bounded to 2 on the compiled graph); `xtb_job_timeout_seconds` 14400→15000 +
      validator over `calc_sampling_timeout_seconds` (equality was the defect); calc manifest
      `request_timeout` 60→600 (a 60 s bound cancelled real work the server was allowed 900 s
      for, uncached); connector open (15 s) and teardown (5 s) bounds in `transport.py`.
- [x] **B — middleware**: loop cap unconditional (ADR: the-cap-is-a-property-of-the-loop…);
      transport failures worded transient via `connectors.transport.transport_failure` (layering:
      the predicate lives where mcp/httpx are legal imports); audit rows batched off the
      tool-call path in `PostgresAuditSink` + runner turn-end flush; `_truncate` bounded via
      reprlib; `side_effecting_tools` `@cache`d (cleared beside the discovery caches);
      repeat-guard forgiveness keyed by cleared *call id*, once per turn (it was re-fired every
      model call past 30k tokens, disarming the guard); compaction metrics high-water-marked;
      plan gate judges a batched call against the plan the batch writes; approval consumption
      session-wide + on abandonment when the turn acted (ADR: the-approval-follows-the-turn…);
      verifier evidence budget (`verifier_evidence_max_chars`, newest-first, omitted ids named).
- [x] **B declined in-flight**: a first-party orphaned-`tool_use` repair — deepagents'
      `PatchToolCallsMiddleware` already heals dangling and invalid calls in `before_agent`;
      pinned in `test_upstream_surface.py` instead of duplicated. Found and fixed en route: the
      parallel-batch false positive in `calls_without_adjacent_results`.
- [x] **C — runner/durable**: Temporal probe gathered with the connector open;
      `job_completed`/`job_failed` mailbox claimed at turn start and framed into the model's
      input (ADR: the-mailbox-reaches-the-model…); tailer restores an undelivered claim;
      `chemclaw_pushback_dropped_total` + `chemclaw_rejoin_describe_failed_total`;
      `get_durable_job_status` long-polls `job_status_wait_seconds`; astream 3-tuple arity pinned
      in `test_upstream_surface.py`; `cached_compute` in-process single-flight (8 misses → 1
      compute; DEFERRED row narrowed to the cross-process half).
- [x] **D — identity under parallel batches**: measured, not guarded — two concurrent
      `tools/call`s on one MCP session each read their own caller on this SDK; pinned in both
      repos (`tests/test_connector_identity.py` here, `tests/test_identity_contract.py` in the
      fleet) rather than defended with dead code.
- [x] **E — detach ≠ stop** (ADR: a-disconnect-is-a-detach-not-a-stop): `api/detach.py` pump +
      registry, `POST /sessions/{id}/turn/stop` (owner-gated, in the session-route inventory),
      `service_turn_survives_disconnect`; end-to-end tests over a real uvicorn socket (TestClient
      and the ASGI transport buffer, so they cannot express a mid-stream drop — recorded in the
      test's own docstring). UI: Stop posts the stop route then aborts; an accidental drop polls
      the transcript back instead of a dead-end banner.
- [x] **Chart**: retention posture must be stated to render (`retention.windows` xor
      `retention.unboundedGrowthAccepted`), mirroring the egress refusal — the code defaults
      stay 0/off deliberately (a disposal policy is a deployment's statement, per
      `core/config/memory.py`'s own argument), so the chart is where the silence had to stop.
- [x] Docs: four ADRs + ledger rows; DEFERRED rows narrowed/added; CLAUDE.md + ARCHITECTURE.md
      race prose updated; `.env.example` for every new setting.
- [x] Final gate: `make lint type test` + prose validators, then push all three repos.

## Deliberately not done, and why

- **Compaction estimator memoization / upstream's per-call deep copy**: [risk]-grade cost
  (char/4 over ≤100k tokens per call, milliseconds); the behavioural harms that made it matter
  (repeat-guard reset, metric inflation) are fixed. Re-open if a profile measures model-call
  overhead worth it.
- **Turn-scoped caching of the plan-gate's checkpoint fallback**: the fallback only fires inside
  subagents/pre-first-write turns, and caching a plan across a batch is a race against
  `write_todos` that a security gate should not run. The turn-end read is deleted outright
  instead (consume_all needs no hash).
- **Lazy compile of the `task` helper (61 ms/turn)**: upstream consumes the compiled runnable
  directly; a lazy proxy is a coupling to how `SubAgentMiddleware` invokes it — measured cost
  does not justify a new unpromised-shape dependency.
- **`invalid_tool_calls` surfacing and the prompt-prefix ceiling rows**: pre-existing BACKLOG
  rows with their own constraints (live-lane gates); not claimed here. Note upstream's
  `PatchToolCallsMiddleware` now answers dangling invalid calls at the *next turn's* start,
  which partially narrows the first row.

## Review

The audit's ranked list is implemented 14/15 with one reshaped (orphan repair → upstream pin) and
one consciously partial (per-request latency work under #15: probe gathered + handshake/teardown
bounds + audit off-path; cross-turn MCP session pooling stays declined until the per-turn-session
rationale is re-argued). Everything landed with a failing-first or mutation-checked test; the two
measurement-first rules paid off twice — `max_concurrency` was verified against the installed
LangGraph before the setting existed, and the caller re-binding "risk" dissolved under its own
test and shipped as a pin instead of a guard.
