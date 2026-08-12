# D-2026-08-11-what-the-review-found-in-the-compaction-change — a middleware that narrowed its engine, a privacy flag that re-armed itself, and a placeholder arguing with a guard

**Status:** accepted · **Date:** 2026-08-11 ·
**Amends:** [D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has](D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has.md)
and [D-2026-08-11-a-model-call-is-a-span-and-phoenix-is-a-deployment](D-2026-08-11-a-model-call-is-a-span-and-phoenix-is-a-deployment.md)

## Context

Those two ADRs shipped together and were reviewed afterwards, at the review skill's highest effort
plus a hand pass over the interactions a diff-shaped review does not see. Six findings from the
first, three from the second. Three are worth an ADR because they change behaviour or contradict a
merged decision; the rest are recorded in the PR and the code.

**The pattern in all three is the one the original ADR was about**, which is the uncomfortable part:
a mechanism whose description and whose behaviour had drifted apart. Writing the fix for that class
of defect did not make the change immune to it.

## 1. The observer narrowed the engine it was reporting on

`record_context_compaction` was a `@wrap_model_call`-decorated `async def`, and its docstring argued
for that: "async, like every other first-party middleware here, because the hook a turn takes is the
async one". The argument was right about turns and wrong about the middleware system.
`AgentMiddleware` raises `NotImplementedError` for whichever half a middleware leaves undeclared,
and `create_agent` puts a middleware declaring *either* hook into *both* chains — so attaching it
made every synchronous `graph.invoke()` and `graph.stream()` fail.

Measured: `build_langgraph_agent(model=fake).invoke(...)` raised "Synchronous implementation of
wrap_model_call is not available", while the same graph without the observer answered. The reachable
caller is `agent/team.py::_AttributedSpecialist.invoke` — deepagents' `task` tool carries a sync
`func` beside its coroutine — so this was not hypothetical, merely dormant behind
`agent_teams_enabled`.

**`RecordContextCompaction` is now an `AgentMiddleware` subclass declaring both hooks** over the
same `_record_reduction`. `ContextEditingMiddleware` above it declares both for the same reason. An
observer that disabled an engine in order to report on it is the worst available version of "the
metric exists so the policy is checkable rather than believed".

## 2. Reviving a privacy flag re-armed it on upgrade, silently

`otel_include_sensitive_data` was given its meaning back rather than adding a second knob, and that
part stands. What the change did not account for is that the flag had been **inert for a phase**,
and the config comment kept it explicitly "because a deployment may still have it in its values
file". So deployments hold a stale `true` by the repo's own reasoning — and the same change ships
`CHEMCLAW_OTEL_LLM_SPANS: "true"` in the chart. On upgrade, such a deployment starts exporting a
chemist's question and the model's answer to its collector, with nobody having decided that in this
release.

The process warned in exactly the harmless direction (flag set, spans off — "has no effect") and
said **nothing** in the harmful one. That asymmetry was the defect: the warning existed because an
enabled-but-ineffective privacy switch reads as an effective one, and giving the flag a consumer
inverted which case needs saying.

**`_warn_about_sensitive_data` now speaks in both directions**, and the live-case line names the
endpoint the content is going to, because a warning that omits *where* leaves the operator with the
wrong half of the question. Not an error and not a refusal — a deployment is entitled to make this
choice, and failing to start over a telemetry setting would be worse than the setting. What it is
not entitled to is making it without noticing.

## 3. The placeholder was arguing with the repeat guard, at the worst possible price

The merged ADR says a cleared tool result "carries a placeholder that says what happened **and that
the tool can be re-run**", reasoning that an unexplained placeholder would read as a tool returning
nothing and provoke a repeat the guard would refuse. The reasoning identified the right interaction
and drew the wrong conclusion from it: *explaining* does not stop `repeat_guard` refusing. Inside one
long harness turn a cleared result can be re-fetched, cleared again and re-fetched, and the third
identical call in a turn is refused — so the placeholder was instructing the model to do the thing a
guard three middlewares away would then deny.

It also charged for the advice in the worst place. The string is repeated once per cleared result,
tens of times, in exactly the situation where the budget is already spent.

**The placeholder now states the fact and gives no instruction**, and the guidance is paid for once
in the system prompt — which is where the marker is already explained, and where a sentence costs
one copy rather than twenty. The prompt now says the model *may* re-run the tool but should prefer
what is still in view, because a re-fetched result is dropped again once the budget is spent and
repeating one tool's identical question is refused. That is the two mechanisms agreeing instead of
contradicting.

## The rest, without ADR weight

- `_plan_command`'s `saver` had a `None` default that fell back to the configured checkpointer —
  the exact defect the saver-threading was written to fix, left reachable by omission. Now required.
- `_prune_checkpoints` capped its batch and reported no remainder, so a capped first pass read as a
  drained backlog. `RetentionOutcome.threads_deferred` is a separate field from `sessions_deferred`
  because the two caps bound different units.
- `leaver._existing_tables` had become a one-caller pass-through after the helper moved to
  `core.db`; inlined.
- `docs/guides/harness-konzept.md` documented `ChemclawState.awaiting_jobs` as live after it was
  deleted, and named two middlewares `lg_loop_cap` and `lg_enforce_plan_approval` — a prefix M13
  removed, so six references pointed at functions that do not exist. `make prose-validate` cannot
  see this: it checks tools, note types, paths, ADR ids, config keys and metrics, not function
  names.
- The prompt-cache row in `DEFERRED.md` tells an operator to read `chemclaw_cache_read_tokens_total`
  against `chemclaw_input_tokens_total`. Above the budget, compaction rewrites the front of the
  message list every call, so the cacheable prefix changes by construction and a low ratio measured
  there says nothing about the provider. The row now says so.
- `_prune_checkpoints` casts `checkpoint->>'ts'` to `timestamptz` with no `TRY_CAST` available. A
  malformed payload fails the pass loudly, and that is now written down as the answer rather than
  left as silence: `checkpoints` is last in `_PRUNABLE` and every earlier table commits in its own
  statement, so the pass keeps what it already disposed of, and swallowing the error would turn a
  disposal job that *cannot run* into one that reports success while a table grows.

## One thing the review asked that the change had not answered

**The chart turns the instrumentation on by default, so what happens if it misbehaves in
production?** Checked rather than assumed: `LangChainInstrumentor` works through a callback handler
(`OpenInferenceTracer`, a `BaseCallbackHandler`), and `langchain_core.callbacks.manager.handle_event`
catches `Exception` from a handler, logs a warning, and re-raises only when that handler sets
`raise_error` — which `OpenInferenceTracer` leaves at its class default of `False`. So a bug in the
instrumentation costs a log line and a missing span, not a chemist's turn. That is the property that
makes `CHEMCLAW_OTEL_LLM_SPANS: "true"` an acceptable chart default rather than a bet, and it is
recorded here because it is not obvious from either package.

## Consequences

- The suppression-list omission found by `test_every_hide_flag_is_set_together`, the sync-hook
  defect found by the review, and the placeholder contradiction found by hand were each found by a
  *different* method. That is the argument for running all three rather than trusting whichever one
  is cheapest.
- Nothing here changes the measurements in the two amended ADRs: the compaction table, the
  deep-copy cost and the Phoenix span counts were all re-verified after these fixes.
