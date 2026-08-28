# D-2026-08-27-a-refusal-is-not-a-crash — the agent layer records what it decided, not only that something happened

## Status

Accepted. Found in a logging/monitoring review of `src/chemclaw/agent/`, and fixed in the same pass.

## Context

The agent layer's audit trail is a good forensic record and was a poor diagnostic instrument.
Sixty-six declared metric series, and not one of them counted a refusal, a model call, a model
error, a provider rate limit, or a checkpointer failure. Each finding below was measured on the
tree, not inferred from it.

**A refusal was indistinguishable from a crash.** Five governance gates — authorization, the
dry-run guard, the undeclared-write refusal, the plan gate and the repeat guard — stop a tool call
by raising, so all five landed in `agent/audit.py`'s `except Exception` as `outcome='error'`, beside
a `KeyError` from a parser. The log line was worse than the row: it interpolated `%s` on the
exception *instance*, so the class was lost entirely, while `_truncate` reprs a non-string and the
**row** kept it. The database was therefore strictly more diagnostic than the log — the inverse of
this module's own opening rule that "the log is the floor, the sink is the durable record". Four of
the five gates moved no metric at all; the fifth counted only itself.

**A tool that returned a failure produced a span that said nothing was wrong.** Measured on
`chemclaw.tool`: clean = `UNSET`, raised = `ERROR`, `CancelledError` = `UNSET`, **returned error =
`UNSET`**. The `with start_span(...)` block exited cleanly before the returned-error branch ran, and
`use_span` catches `Exception` rather than `BaseException`, so the cancellation set nothing either.
CLAUDE.md records that an MCP tool *never raises*, which means essentially every connector-tool
failure in production was an `UNSET` span while the audit row said `error`. An operator filtering a
collector by `status=ERROR` saw none of them.

**The latency histogram could not name a tool.** `chemclaw_tool_duration_seconds` pooled a
minutes-long xTB call through the calc connector with a sub-millisecond `read_attachment`, so
per-tool p95 — the first number anyone wants for "why is this turn slow" — did not exist.

**There was no LLM error taxonomy anywhere: no metric, no log, no span.** `agent/llm_provider.py`
constructs a client and returns it; nothing wrapped a model call. `max_retries=llm_max_retries`
(3 by default) is passed *into* the provider SDK, so one `ainvoke` covers up to four wire attempts
with no callback — a deployment retrying every call three times looked identical to one retrying
none. A provider 429 had no counter distinct from the front door's own *inbound* limiter. And a
context-length `BadRequestError` fell through `api/runner._classify` to `("internal", False)`, so the
one failure mode `agent/compaction.py` exists to prevent was unmeasurable, and the chemist was told
"internal error, do not retry" about the one failure a shorter question fixes.

**Provider failover was silent in both directions.** `RunnableWithFallbacks.ainvoke` catches the
primary's exception and moves on with no log line and no callback for the failed attempt, so the
primary endpoint dying and the fallback absorbing 100% of traffic produced nothing at all — for a
feature whose entire operational value is knowing whether it has fired.

**A checkpointer outage was classified as a non-retryable internal error.** `psycopg_pool.PoolTimeout`'s
MRO is `PoolTimeout → OperationalError → DatabaseError → Error → Exception`: neither `ConnectionError`
nor `TimeoutError`, which are exactly the two types `api/runner._classify` tests. `core/db.connection()`
translates for that reason at both of its connect paths; the checkpointer's autocommit pool bypasses
them by design, so **the one Postgres pool that is not `core/db`'s was the one whose outage told a
chemist not to retry** — about the most retryable failure this system has. Nothing counted a
checkpoint write failure, and mid-turn it is silent loss of the turn's state.

**One compaction counter answered neither compaction question.** The middleware composes two edits
with opposite consequences: `ClearToolUsesEdit` is lossless (the model can re-fetch) and
`KeepLastConversationGroupsEdit` is destructive (conversation turns are deleted from what the model
sees). "The agent forgot what I told it three turns ago" and "the agent re-ran a tool it already
ran" *are* those two edits, and `chemclaw_context_compactions_total` is unlabelled. `_cleared_calls`
already computed `(call_id, tool_name, args)` per cleared result and threw it away. There was also
no `try` anywhere in `agent/compaction.py`, so a raising edit killed the turn as a generic internal
error — losing the answer, the tokens already spent and every tool the turn had run, in order to
save tokens.

**Skills were entirely uninstrumented.** `agent/skill_backend.py` and `agent/skill_access.py`
contained zero `logger.` calls and zero metric calls. "The agent is not following the procedure" is
a top-three support question and its first step — *was the skill even offered, and did the model
read it?* — had no answer. A role-gated read refused on the backend, which is the enforcement point,
was completely silent.

**The trail could not say which plan step a call served, and its ordering was the flusher's.**
`job_records.plan_step` exists (migration 057) and `stamp_plan_link` binds the step ambiently, but
`audit_events` — the row written for *every* tool call, most of which launch no job — had no such
column. And `PostgresAuditSink.record` buffers and returns, so `audit_events.ts` defaulted to
`now()` at INSERT and `id` is a `BIGSERIAL` assigned at the same moment: under load, both the
timestamps and the ordering `chemclaw explain` reconstructs a turn from belonged to the flusher.

**An unparseable tool call was a silent no-op** (the standing `BACKLOG.md` row). LangChain puts a
tool call whose arguments do not parse onto `AIMessage.invalid_tool_calls`; nothing in `src/` read
that field, and the agent iterates `tool_calls`. So the call vanished — no `tool_failed`, no
`tool_result`, no audit row, no span. With no prose the turn ended as `empty_answer`; with prose it
proceeded as though no tool had been needed.

## Decision

**The audit trail gains a fourth outcome, and the classification is by type.** `refused` sits beside
`ok`, `error` and `cancelled`. `agent/audit.refusal_reason` maps the five gate exception types to
the five reasons `chemclaw_tool_refusals_total{reason}` declares, most specific first. The
classification and both counters live in `_recording` — **one site, inside the middleware every call
passes through** — rather than in the five gates, because a gate that has to remember to count
itself is a gate that eventually does not. The column has no `CHECK` behind it (migration 006 says
so deliberately), so this needs no migration. The log line now carries `type(exc).__name__` beside
the message, on both the raised and the returned-failure paths.

**`chemclaw_tool_calls_total{tool,outcome}` and a labelled `chemclaw_tool_duration_seconds{tool}`**
are booked from that same site, so every exit path counts exactly once and none can be forgotten.
A cancelled call is counted under `outcome="cancelled"`, which the declared HELP text does not
enumerate; under-counting *attempted* calls is the worse failure, and the HELP text is the half that
should move.

**`core.tracing.start_span` yields a `SpanHandle`**, so a block can mark what it learned. `_recording`
stamps an `outcome` attribute on every exit path and sets `Status(ERROR, …)` on the two failures that
do not raise — a returned failure and a cancellation. A **refusal is deliberately not marked ERROR
by us**: it raises, so OpenTelemetry marks it anyway, and the `outcome` attribute is what lets an
operator take policy decisions back out of an error view. `correlation.id` joins the span to the
trail.

**One `wrap_model_call` middleware records the model call** (`agent/model_calls.RecordModelCalls`),
booking `chemclaw_model_calls_total{provider,outcome}` and
`chemclaw_model_call_duration_seconds{provider}` with a WARNING per non-`ok` outcome carrying the
provider and the exception class — never the provider's message, which can quote the request. The
taxonomy is **not invented**: `llm_provider._failover_exceptions` already knew which failures mean
"this endpoint is down", because failover depends on it, and `classify_model_failure` reuses that
set as its `transport` family with `timeout`, `rate_limited` and `context_length` named beside it.

What that still cannot see is stated rather than implied, here and in the module: the SDK's retries
happen below `ainvoke`, so **one recorded call is between one and `llm_max_retries + 1` wire
attempts** and this cannot say which. Observing it means either `max_retries=0` plus a first-party
backoff loop or an httpx event hook, and neither is done — so the retry budget remains configured
and unmeasured.

**Failover is counted by hooking its consequence, because its cause is unhookable.**
`RunnableWithFallbacks` offers no callback for the attempt that failed, so the *fallback* model is
constructed with a `BaseCallbackHandler` attached: the fallback is invoked only after the primary
raised, so one `on_chat_model_start` is exactly one failover, with no inference.
`chemclaw_model_fallbacks_total{provider}` and a WARNING.

**`SchemaStampedSaver.aput` translates `psycopg.Error` to `ConnectionError`** — the same translation
`core/db.py` makes and for the same reason, so a caller classifying a database outage cannot get a
different answer depending on which pool the statement went through — and counts it first through
`degraded(logger, "checkpointer", …)`. This is the one `degraded` site that does not swallow: the
write did not happen, so the caller must still fail.

**Compaction publishes what it removed, once per turn.** A label cannot be added to an
already-declared counter from the agent side, so the distinction lands as a structured record: one
`context.compacted` event on the high-water reduction, naming the reclaimed tokens, the number of
tool results cleared **and the tools they belonged to**, and how many conversation groups were
dropped. Counts and names, never content. The window edit's own per-model-call INFO drops to DEBUG,
because it re-derives the same standing cut on every call. `GuardedEdit` wraps *both* edits and
`_record_reduction` guards itself, so a failure degrades and the call proceeds **uncompacted** —
which is the direction with a chance of being answered, since a request over budget fails with a
provider error that is now classified and told to the chemist as such.

**Skills are instrumented at the three points that answer the question**: a DEBUG at build time
naming the skills this profile offers and how many the predicates removed; an INFO per skill body
read; and a WARNING plus `chemclaw_skill_reads_denied_total` on a refused read.

**`audit_events` gains `plan_step` and an index on `(tool, outcome, ts)`** (migration 059), and `ts`
is stamped in `_recording` when the call *starts* rather than defaulted by the INSERT. The step is
read from `request.state["todos"]` through the *same* `plan_link_from_todos` a job is stamped with —
not from the ambient contextvar, which was measured to read `("", "")` at the moment the row is
written, because `stamp_plan_link` sits innermost and resets in a `finally` while the audit
middleware is outermost. Reading the request is also the **wider** answer: a refused call never
reaches `stamp_plan_link` at all, and a refusal is the row an operator most wants a step on.

**`purpose` is dropped from `chemclaw explain`'s projection rather than filled from `plan_step`.**
They are different questions — one promises a *reason*, the other is a position in a list — and
copying one into the other is exactly the inference that field's own docstring declines. The column
and the field stay (the schema is forward-only); what changes is that the operator tool renders the
answerable question instead of a structurally blank one. `explain` also orders by `ts` before `id`.

**An unparseable tool call is repaired once, from `wrap_model_call`.**
`agent/model_calls.RepairInvalidToolCalls` counts `chemclaw_invalid_tool_calls_total{tool}`, logs
what could not be parsed, and asks the model again with a corrective instruction appended to the
*request only* — so the discarded attempt never reaches graph state, the transcript or the
checkpoint. Exactly one retry: a second unparseable reply is returned with an ERROR beside it,
because returning the first instead would be choosing the reply that is known to be broken.

**It does not jump from `after_model`**, which is the constraint
`D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped` leaves behind: a middleware
jumping from there short-circuits every middleware that runs later, and the loop cap is one of them.
A jump back to the model would have bought a correction by disarming the runaway guard.

## Consequences

- A log query can separate a governance refusal from a fault, and a metric query can do it without
  reading logs at all. `chemclaw_tool_refusals_total{reason}` is the series an operator watches;
  `outcome='refused'` is what an auditor reads.
- Per-tool p95 exists. So does per-provider model-call latency and a per-outcome model-call rate.
- A collector filtered on `status=ERROR` now shows connector-tool failures, which were its single
  largest blind spot.
- A checkpointer outage tells the chemist to retry, and moves a counter while it does.
- The turn's reconstruction (`chemclaw explain`) is ordered by when tools ran and says which plan
  step each served.
- One extra model call is spent per malformed tool-call emission. It is counted
  (`chemclaw_model_calls_total` books both attempts, because both happened), bounded at one, and
  paid only on a failure that previously produced a silently wrong turn.
- `chemclaw_tool_calls_total`'s declared HELP text enumerates three outcome values and four are
  produced. `core/metrics.py` is owned by another workstream in this pass; the HELP text is the
  thing to correct, not the producer.

## What was deliberately not changed

The loop cap's design (measured rather than inferred, and the model the rest of this follows);
`_book_turn_spend`'s no-await `finally`; `graph_usage_tokens.unreadable`; the `cancelled` audit
outcome and its shielded write; the `returned_failure` classification; and `audit_events.agent`
staying empty behind its absence test
(`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`).

`api/runner._classify` is untouched: the checkpointer fix works on the *raising* side, which is
where the analogous `core/db.py` translation already lives, so there is one rule about what a
database outage looks like rather than two.
