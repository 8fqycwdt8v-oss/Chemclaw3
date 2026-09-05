# D-2026-09-05-a-refusal-for-capacity-is-not-a-refusal-of-the-question — saturation is a third category, not bad data

**Context.** A ten-track performance review against 200 concurrent users found the same defect from
three independent directions — deployment sizing, the MCP client's error taxonomy, and the durable
retry policy. Traced end to end and reproduced:

```
servers/calc/engine/admission.py  raises ValueError("… Retry once one finishes …")
  -> mcp_server_kit marks it caller-safe, outcome="refused"
  -> core/mcp_session.py                McpRequestRefused
  -> connectors/calc/remote.py          CalcToolError
  -> durable/publish.py _BAD_DATA_TYPES  ->  NON-RETRYABLE
```

`durable/publish.py` states its intent exactly — "an unparameterised solvent, an atom index past the
molecule, a SMILES outside a predictor's domain" — and deliberately excludes `CalcServerError`,
because an unreachable server *is* the fault a retry fixes. Both sides were individually right.
**The taxonomy had two buckets, `refused` and `broke`, and saturation is a third one it did not
have.** Under load "pod full" is the normal state — one pod, four slots, and a CREST search charged
all four — so a chemist's durable job failed permanently on its first attempt, carrying the serving
side's own advice to retry.

**Decision.**

1. *Saturation is its own category, on both sides of the wire.* `AtCapacityError` and a marker at
   the head of the message on the serving side; `McpAtCapacity` and `CalcBusyError`
   (a `SubsystemUnavailableError`, the hierarchy `tests/test_publish.py` asserts is *absent* from
   `_BAD_DATA_TYPES`) on the calling side, so it is retryable by construction rather than by a list
   entry someone must remember.

   **A marker in the text, because the protocol carries nothing else.** Measured against the running
   server rather than assumed: `_make_error_result` builds `CallToolResult(content=[TextContent],
   isError=True)` with `structuredContent: None`, and FastMCP's `Tool.run` has already flattened
   every exception type into one `ToolError`. This is the same mechanism the kit already uses for
   "an internal error occurred". `mcp_server_kit` needed no change.

2. *The backoff carries its own jitter, because Temporal has none.* Measured on the real broker:
   1.016 / 2.013 / 4.015 / 8.021 s against nominal 1/2/4/8. Without jitter every job a full pod
   refused together would return together. Spread downward only, per-run and replay-deterministic.

3. *A bundle queue's wait is bounded, at its own scale.* `D-2026-08-27-a-start-to-close-timeout-does-not-bound-the-wait`
   scoped its rule to `durable/` and argued the exclusion — on a bundle queue a wait genuinely *is*
   backpressure. That argument is still right and is not an argument for no bound: measured at 200
   users the only ceiling was the child's five hours, so a job that never got a slot told the
   chemist "running" and then failed as a workflow-execution timeout, which is delivered to nobody
   and names neither the queue nor the reason. The bound is half the job's own budget — above the
   measured p95 backpressure of ~1.98 h, below the parent ceiling. Eight call sites, and the AST
   scan now covers `connectors/` so a future bundle cannot omit it.

4. *The 60-second drops are bounded separately from the work they cannot control.* Push-back and the
   job record both carried `schedule_to_close_timeout = 60 s` on the queue that also runs 900 s
   template steps; measured, eight slots held by long activities made a 50 ms activity wait 41.6 s
   and return `Activity task timed out` at 60.1 s. Both failures were swallowed best-effort, so the
   session showed "running" for ever and `job_records` — the only copy outliving Temporal history —
   was lost. Now a `schedule_to_start` bound, with the work bound unchanged and the retry budget
   restored.

   **A third, tighter bound rather than core's hour, and an existing test is why.** The first
   attempt used core's, and `test_a_failed_job_reaches_its_session_even_with_the_record_queue_unserved`
   went red: the job record sits *in front of* the failure notification, so an hour there is an hour
   in which a failed job tells nobody.

**What it costs, stated because it is a real behavioural change.** A durable calc job against a
saturated backend now spends up to ~28 minutes in backoff before failing, where it used to fail
instantly. That is the fix rather than a side effect — the instant failure was wrong — but it does
mean a genuinely dark backend surfaces more slowly. `chemclaw_calc_backend_at_capacity_total{tool}`
is what separates *busy* from *dark*, and it is deliberately **not** on `chemclaw_degraded_total`,
which would fire the outage alert on ordinary busy-ness and make the one series an operator trusts
for "the backend is down" mean two things. No replica, permit or resource change.

**Deliberately not done.** Cross-process single-flight in `cached_compute`, whose `DEFERRED.md`
trigger this load was expected to trip. It does not: the shipped process count is two, Temporal's
workflow-id reuse already collapses the dominant case, and the obvious remedy is measurably wrong —
with the pool and the activity ceiling both at 8, holding eight advisory locks starved an ordinary
query to `PoolTimeout` after 5.00 s against live Postgres. The row stands with its trigger.

**Two queue bounds are derived, not settings.** They are fractions of `connector_job_timeout_seconds`
and `template_step_timeout_seconds` with the derivation written down. Promoting them to
ENV-overridable settings was proposed and declined: it would let an operator set a queue bound
*above* the job budget it must stay below, which the derived form cannot express, and which is
exactly the class of contradiction the fleet cross-checks in `core/config` exist to catch after the
fact.

**Amends** `D-2026-08-27-a-start-to-close-timeout-does-not-bound-the-wait`: §3 extends its rule to
bundle queues at a different scale, and §4 replaces the `schedule_to_close` exception it records for
`durable/notify.py`. That ADR is not edited.
