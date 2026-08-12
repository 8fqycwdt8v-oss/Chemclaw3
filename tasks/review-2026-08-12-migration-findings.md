# Migration findings, ranked (2026-08-12)

Phases 2 and 4 of the full review sweep: what the MAF→LangGraph migration implies for correctness,
and what to do about it, ranked by **impact × safety** rather than impact alone.

Companion documents: `review-2026-08-12-maf-removal.md` (Phase 1),
`review-2026-08-12-langgraph-native.md` (Phase 3).

**Evidence standard.** Every finding below names a file:line and, where the claim is behavioural, a
measurement taken by running it. Findings that could not be settled statically are marked
**unverified** and say what they need. Vindications are recorded too — code that is right for a good
reason is a finding, and the ones here are numerous.

---

## The headline

**`run_agent_step` — a Temporal activity that re-enters layer 1 — runs with no plan gate, no loop
cap, no repeat guard, no cost ledger and no idempotency, over 16 state-changing tools, wrapped in a
5-attempt retry policy.** Four independent controls that hold on a chat turn do not hold there. Every
finding in tier 1 is a consequence of that one seam.

This is the migration's shape showing through: `api/runner.py` grew the controls, and the *other*
caller of the same graph did not.

---

## Tier 1 — high impact, safe to fix

### T-1 · The plan gate does not apply to template agent steps

`gate_applies(profile) = harness_enabled_for(profile) and autonomy_for(profile) == PLAN_ONLY`
(`agent/plan_gate.py`), and `_classic()` returns the profile with `harness_enabled=False`
(`durable/template_activities.py:410-419`). `enforce_plan_approval` is attached only when
`gate_applies` (`agent/langgraph_agent.py:440`); `_harness_middleware` returns `[]` when the harness
is off (`:254-256`).

Measured against **the chart's own shipped posture** (`harness_enabled=true`,
`harness_autonomy=plan_only`, `deploy/helm/chemclaw/values.yaml`):

```
a chat turn:                gate_applies = True
run_agent_step (_classic):  gate_applies = False
_harness_middleware(_classic) = []      ← no TodoListMiddleware, no enforce_loop_cap
in-process tools on the step's surface: 28
  state-changing / durable-launching among them: 16
    compute_dft_energy, propose_knowledge_note, start_optimization_campaign,
    request_development_report, record_confirmed_answer, remember_preference,
    forget_preference, record_failure, run_hazard_briefing, sample_conformers,
    scan_coordinate, compare_solvents, compute_interaction_energy,
    compute_reaction_energy, watch_for, stop_watching
```

Two further controls are silently inert on this path, for a different reason — they are armed by
context managers that only `api/runner.py` enters: `refuse_repeated_calls` no-ops because
`begin_call_watch()` is called only at `api/runner.py:197` (`repeat_guard.py:99-100` returns `None`
when the contextvar is unset), and `loop_hit_cap()` likewise (`api/runner.py:201`).

**Impact:** the GxP "AI proposes, human signs off" line is the repo's central control, and there is a
path around it. **Safety of fixing:** high — attaching the gate to this path is additive.
**Recommendation:** decide deliberately whether a template step should be gated, and make the answer
explicit rather than a side effect of `_classic`. This one wants an ADR, not a patch.

### T-2 · Template agent steps are unmetered — a deployment that looks free and is not

`record_turn_cost`, `chemclaw_tokens_total`, `budget.record`, `begin_call_watch` and
`begin_loop_watch` have **exactly one caller each — `api/runner.py`** (`:527`, `:554`, `:509`,
`:197`, `:201`). `durable/template_activities.py` calls none of them, and `graph.ainvoke` at `:384`
never reads `usage_metadata` (`runner_usage.graph_usage_tokens` is reachable only from
`api/graph_stream.py:122`).

So a template `agent` step's token spend reaches **neither `turn_costs`, nor the Prometheus token
counters, nor the `BudgetTracker` the chart ships enabled** (`values.yaml:386`
`CHEMCLAW_BUDGET_ENABLED: "true"`). Multiply by up to 5 retry attempts. `otel_llm_spans` is off by
default, so there is no fallback observation either.

This is the exact class D-144 records ("a deployment that looks free and is not"), re-opened by a
path the migration did not re-check. **Safety of fixing:** high — metering is additive.

### T-3 · `recursion_limit` is inherited, not chosen — ~5,400 model calls per turn

Full analysis in the native audit (N-4). Summary: `recursion_limit` is never set; `create_agent`
bakes 9999 (`langchain/agents/factory.py:1779`, verified: `agent.config['recursion_limit'] == 9999`);
measured ~1.83 supersteps per model call, so the ceiling is in the thousands. It fails by **raising**
`GraphRecursionError`, discarding the partial answer — contradicting the position `loop_cap.py:101-103`
states explicitly ("a raised error would discard work a chemist is entitled to see").

Combined with T-1: a template agent step's only real bounds are that ceiling and
`template_step_timeout_seconds` (900 s), **retried 5×** — up to ~75 minutes of unbounded model
looping over 16 write tools per step. **Safety of fixing:** high — setting an explicit limit is
additive.

### T-4 · A mid-turn resume grants a second full loop-cap budget — measured

`graph_events` starts from `turn_input()` (`api/graph_stream.py:117`), which zeroes `model_calls`
(`agent/state.py:84`); `api/runner.py:341-349` calls it a second time on the same graph and
`thread_id`. Measured on a real `create_agent` graph with `enforce_loop_cap` and a cap of 2:

```
pass 1: model calls = 2, state.model_calls = 2, loop_capped = True
pass 2: model calls = 2, state.model_calls = 2, loop_capped = True
TOTAL model calls for ONE chemist turn = 4   (cap is 2)
```

One chemist turn can make `2 × harness_max_loop_iterations` calls (default 25 → 50). This is the
mirror image of the defect D-2026-08-12 §1 fixed: `turn_input` correctly made the counter
per-*invocation*, and the resume made a turn two invocations. Nobody re-derived what "per turn" meant
after AGT-2 was wired.

Second consequence, same root: `loop_capped` is a boolean, so a turn capped in pass 1 that then
resumes and answers cleanly still emits `loop_cap_reached` and marks a complete answer partial
(`api/runner.py:361-379`).

**Severity is bounded today** — `mid_turn_resume_enabled` defaults `False`. **The fix is N-1**:
declaring the field `Annotated[int, UntrackedValue]` makes this unexpressible rather than guarded.

### T-5 · LangSmith egress is pinned in one launcher out of several

`langsmith` 0.10.17 is in the runtime closure (a hard requirement of `langchain-core`, pulled again
by `deepagents`). Measured:

```
LANGSMITH_TRACING=true  → langsmith.utils.tracing_is_enabled() = True
unset                   → False
```

`langsmith/utils.py`: `get_env_var("TRACING_V2", default=get_env_var("TRACING", default=""))`. No
repo code is involved.

The repo pins both names false **only in the Helm chart** (`_helpers.tpl:40-42`), beside a
NetworkPolicy. There is **no in-process guard** (`grep LANGSMITH src/` → 0 hits) and nothing in
`deploy/entrypoint.sh`, CI, or the Makefile. So every non-Helm process is unguarded: `make chat`,
`make connectors`, hand-started Temporal workers, local dev, CI, and `docker run` of the image —
which `image.yml` itself does for its smoke tests.

The chart's own comment makes the argument better than I can: *"It is off by default today
(measured), which is exactly the kind of fact that changes in a patch release or gets set by a base
image. A GxP deployment's egress posture should not rest on a library default."* That reasoning
applies to every process, and the control was placed in one of them.

**Fix:** pin it in `deploy/entrypoint.sh` and/or at the composition root, so it holds regardless of
launcher. **Safety:** high — it is a default-deny tightening.

### T-6 · The conversation window does not bound the thread — measured

Full analysis in the native audit (N-2). `KeepLastConversationGroupsEdit.apply` triggers on *tokens*
but cuts by *group count*, and returns early when `len(starts) <= keep`. Measured at shipped
defaults over 20 tool-free prose groups: 240,230 → **144,142** tokens against a 100,000 trigger.
Its docstring claims it "is what makes the thread *bounded*". It reduces; it does not bound — and
the hard provider-context error it exists to prevent still happens. Fix is ~6 lines using
`trim_messages(strategy="last", start_on="human")`, whose pairing safety was measured at every budget
from 2000 down to 250 tokens.

---

### T-14 · There is no timeout on an MCP tool call, and `request_timeout` makes it *worse*

**The most severe operational finding in this sweep, reachable with shipped values.**

The chain, verified at source:

1. `registry.py:290` sets `httpx.Timeout(endpoint.request_timeout, connect=5.0)` — the only timeout
   in the path. The library's own 30 s/300 s defaults are dead, because `_connector_client_factory`
   ignores its arguments by design (`registry.py:377-390`).
2. `registry.py` sets **no** `session_kwargs` (grep: zero hits), so `read_timeout_seconds=None`.
3. `langchain_mcp_adapters/tools.py:489-493` calls `session.call_tool(...)` with **no**
   `read_timeout_seconds`.
4. `mcp/shared/session.py:283-291`: `timeout = None` → `anyio.fail_after(None)` → **waits forever**.
5. `api/routes/turns.py:161`'s `asyncio.timeout(service_turn_timeout_seconds)` — default **600 s** —
   is the only real bound.

**And the httpx read timeout that does fire is swallowed.**
`mcp/client/streamable_http.py:429-434` catches `Exception` (including `httpx.ReadTimeout`) at debug
level and reconnects only if it saw an event carrying an id; FastMCP's response events carry none.
The POST task returns silently having written nothing to the read stream, and because the transport
stays up the caller is never woken by the "Connection closed" path.

Measured against a real FastMCP server behind `connector_app`, with `request_timeout=2.0`:

```
tool sleeps 60 s → still blocked at 20 s, no exception
tool sleeps  4 s → still blocked at 25 s   ← server answered at 4 s; result permanently discarded
control: same 4 s tool, request_timeout=30 → RETURNED after 4.0 s
```

So `request_timeout` is not a timeout — it is a **silent-hang trigger**. Any connector tool slower
than its manifest value wedges the turn for the full 600 s while holding an admission permit and the
session's turn slot. Shipped values: `calc: 60`, `bo: 120`, `chem`/`molfp`/`rxnfp`/`safety: 30` — and
`registry.py:279-289` itself notes that "an uncached `predict_pka` runs xTB inline". **One slow pKa
saturates one of `service_max_concurrent_turns` permits for ten minutes.**

The registry docstring's claim that `request_timeout` "bounds the read, which is the phase a slow
tool occupies" is true for the phase *before* response headers and false for the phase *after* them —
which is the phase a slow tool actually occupies.

**Cheapest correct fix:** pass `read_timeout_seconds` on the call (or `session_kwargs` at
`registry.py:355`), so the timeout is enforced where the caller is parked rather than in a transport
that swallows it. **Safety: high** — it converts a hang into an error.

*Vindication in the same area:* the **connect** phase is correctly bounded. Against a black-hole
server that accepts the POST and never sends headers, `open_connector_specs` returned in **3.1 s**
with `unreachable=['blackhole']` and degraded cleanly.

### T-15 · A failed connector tool is audited as a success

MCP tools never raise. `langchain_mcp_adapters/tools.py:527-535` attaches `handle_tool_error`, so an
MCP `isError=True` is converted **inside** `StructuredTool.ainvoke` and returned as
`ToolMessage(status="error")`. But `agent/audit.py:315` derives `outcome` from control flow — it
records `error` only on a raised exception. Measured: both a domain error and an internal error
**returned** rather than raised.

Consequences, each a divergence from the other two tool kinds:

| | MCP tool | job tool | hand-written |
|---|---|---|---|
| `ToolFailedEvent` in the chemist's stream | **never** (`announce_tool_failures` only catches raises) | yes | yes |
| audit outcome | **`ok`** | `error` | `error` |
| domain error to model | `status="error"` | `"Error: …"`, no status | same |

**A chemist asking "what did this turn actually do" sees a clean GxP audit row for a call that
failed.** And D-2026-08-12's `surface_domain_errors` fix — which made a failed tool a recoverable
step again — covers two of the three tool kinds; MCP failures bypass it entirely.

### T-16 · Connector domain refusals invert the repo's own retry policy

`tool_authz.py:112-117` states the case precisely: `status="error"` reaches Anthropic as `is_error`,
"which invites exactly the retry a deliberately-worded refusal is trying to prevent". The connector
path — which carries most domain refusals, e.g. *"that SMILES has an unclosed ring"* — sets exactly
that flag. The policy holds on the two in-process kinds and is inverted on the third.

---

## Tier 2 — real, but the fix is a decision rather than a patch

### T-7 · Two retry systems multiply, and the fix is to remove a layer, not add one

`llm_max_retries` default 3 (`core/config/llm.py:43`) — and **`max_retries=3` means 4 HTTP
attempts**, not 3 (`anthropic/_base_client.py:1132`, `for retries_taken in range(max_retries + 1)`).
`activity_max_attempts` default 5 (`core/config/temporal.py:47`) in `BAD_DATA_RETRY`
(`durable/publish.py:123`), wrapping `run_agent_step` (`durable/template_job.py:157`).

**20 is a floor, not a ceiling.** Each Temporal retry replays the *whole turn* —
`template_activities.py:384` is `graph.ainvoke(turn_input(...))` with no config and no checkpointer —
so the true worst case is `5 × (model calls in the turn) × 4`, and **every tool side effect in the
turn is repeated up to 5×**.

Measured, building exactly what `run_agent_step` builds and failing the model call after a tool had
committed:

```
attempt 1: ConnectionError (not in BAD_DATA_RETRY's list → Temporal retries)
  side effects so far: ['PR opened on branch note/hazard-ccO']
attempt 2: returned normally
  side effects so far: ['PR opened on branch note/hazard-ccO', 'PR opened on branch note/hazard-ccO']

PR-gate branches opened for ONE template step: 2
audit rows for that one logical note proposal: 2
```

**The right change is subtractive.** Layers 2 and 3 retry the same class of failure at wildly
different granularities, and the outer one is the expensive one — it discards a completed turn's work
to re-run one flaky HTTP call. Adding `ModelRetryMiddleware` or a node `RetryPolicy` would be a
*fourth* layer. Narrow the outer policy instead (classify provider-transient errors as non-retryable
at the Temporal boundary, or give `run_agent_step` `maximum_attempts=1..2`) and let the SDK own
transient provider failure. **Trade-off to accept explicitly:** a genuine multi-minute provider
outage then fails the step in ~4 attempts instead of riding it out over 20.

**Not to be touched:** `ingest/documents/sync.py:378,417` is **not** a third retry layer — it is
fan-out-on-failure (batch once; on exception, each chunk once) so a poison chunk costs itself instead
of the corpus. Its own comment records the outage it fixed. Keep unchanged. *(This corrects an
earlier characterisation in the planning document, which called it a third retry layer.)*

### T-8 · Checkpoint pending-writes offer free idempotency that `run_agent_step` cannot use

Measured: on resume from the same `thread_id` after a mid-turn crash, a *completed* task's result was
replayed from `checkpoint_writes` and its tool was **not** re-run — only the failed task
re-executed. That is exactly "make the retry cheap instead of expensive", and it requires a
checkpointer, a stable thread id, and `durability != "exit"`.

`run_agent_step` has none, by an explicit decision (`template_activities.py:377`: *"giving the step
its own checkpointed thread would be a second durability mechanism inside the first (D-002)"*). That
argument is about *where durability lives*; it is not obviously an argument against *idempotency*,
since a Temporal-derived thread id (`workflow_id + step index`) is deterministic under replay and the
rows are prunable by the existing retention sweep.

**This is the one genuine trade worth re-opening, and it is ADR-sized.** ~15 lines plus a retention
rule plus a test — but it re-draws D-002's line, which must be done deliberately rather than crossed
quietly.

### T-9 · A template workflow's failure is invisible to the chemist

`TemplateWorkflow.run` has no `try/except`; `notify_session_best_effort(…, "job_completed", …)`
(`durable/template_job.py:119-129`) is reached only on success. Compare
`ConnectorJobWorkflow._notify_failure` (`durable/connector_job.py:282,333-342`), which pushes
`job_failed` with a reason. So `run_hazard_briefing` returns a workflow id, `record_job_started` draws
a "running" row (`templates/registry.py:209-212`), a step fails after 5 attempts — and **no push-back
of any kind**. The surface shows that run as "running" forever.

### T-10 · No heartbeat on template steps

`durable/template_job.py:139-158` sets `start_to_close_timeout` only — no `heartbeat_timeout` — and
`template_activities.py` never calls `activity.heartbeat`. The repo *has* the idiom
(`durable/heartbeat.py`, used by the calc/qm/bo connector activities), and the one activity that runs
an unbounded agentic loop for up to 900 s does not use it.

**Unverified — needs a live Temporal broker:** whether a start-to-close timeout on a non-heartbeating
activity leaves attempt N's graph running (with live connector sessions and write tools) while
attempt N+1 starts. Static analysis cannot settle it. If it does, that is also the one connector
session-leak case (§ vindication 6 below covers the ordinary paths).

Also unbounded in aggregate: no `schedule_to_close_timeout` and no `execution_timeout` on
`start_workflow` (`templates/registry.py:192-206`), so one agent step's worst case is 5 × 900 s = 75
min and a 3-step template's is unbounded.

---

## Tier 3 — correctness of the record

### T-11 · A claim whose mechanism does not exist — the deterministic call id

`agent/tool_invocation.py:53-60` states:

> "The call id is derived from the tool and its arguments rather than random, so a retried activity
> produces the same id: Temporal retries a step, and an audit trail in which one logical call appears
> under three ids reads as three calls."

**`audit_events` has no call-id column.** Migrations `006`, `010`, `011`, `026`, `044` add none, and
`AuditEvent`'s fields are `actor, agent, arguments, correlation_id, detail, latency_ms, outcome,
purpose, revision, session_id, tool` — measured. The deterministic id reaches only
`ToolMessage(tool_call_id=…)`. **The stated audit benefit does not exist**, and the trail already
appears as three calls for a different reason.

This is the Phase-1 pattern exactly (a claim in a docstring, the mechanism absent), introduced *by*
the migration.

**Related, and worth separating:** duplicate audit rows on retry are **not** corrupting — the hash
chain is intact and each row is truthful, those calls really did happen. But two `ok` rows for
`propose_knowledge_note` with identical `correlation_id`, `tool`, `arguments`, `actor` and
`session_id` are byte-indistinguishable from the model genuinely calling the tool twice. A reviewer
asked "did the agent propose this note once or twice?" cannot answer from the trail.
`activity.info().attempt` is available and read nowhere.

### T-12 · `session_id` is empty on every template-path audit row

`run_agent_step` stamps identity but never calls `set_current_session_id`, so the D-2026-07-31 join
from a tool call back to the question is empty for the whole template path.

### T-13 · Housekeeping

- **Two shipped settings govern nothing** (Phase 1 §5): `calibration_conformal_coverage`,
  `calibration_conformal_min_samples`. Wire them to `science/calc/uncertainty.py:195` or delete them
  — a capability decision, not a cleanup. `service_uvicorn_workers` is a weaker third: no reader, and
  `entrypoint.sh` maps four other settings to uvicorn flags but not this one.
- **`mcp` has no upper bound, though the ratchets do exist.** `connectors/server.py` patches
  `FastMCP._tool_manager.call_tool` twice, stacked (`:279` caller re-binding — a *security* property;
  `:328` error sanitization). **Correcting an overclaim made earlier in this sweep:** I recorded this
  as "a security property with no ratchet", and that is **wrong**. Two behavioural tests drive both
  patches over the real streamable-HTTP transport and fail loudly if either stops applying —
  `tests/test_connector_transport.py:350` (a `RuntimeError` carrying a DSN; a no-op patch leaks it)
  and `tests/test_connector_identity.py:436` (handshake as alice, `tools/call` as bob on one
  session id; asserts the body reads bob — the only test that can see `_bind_caller_per_tool_call`,
  since the single-caller probe at `:204` would pass without the patch). A *renamed* attribute is
  loud too: `AttributeError` at bundle build. What is genuinely missing is the **ceiling**:
  `pyproject.toml:45` is `mcp>=1.2.0`, unbounded, resolving **1.28.1**, while `deepagents` was capped
  `<0.7` on the identical argument (a 0.x package reached through a private surface carrying a
  security property). Given T-14, the SSE-swallow behaviour is version-specific too, which is a
  second reason.
- **Two duplicate migration numbers.** `037_bo_suggestion_provenance.sql` / `037_document_index.sql`
  and `043_session_listing.sql` / `043_session_message_shape.sql`. Apply order within a pair is
  filename collation, not intent.
- **The checkpoint tables are reached by a hand-maintained tuple.** `CHECKPOINT_TABLES`
  (`agent/checkpointer.py:52`) is the only route for both erasure (`agent/leaver.py`) and retention
  (`durable/retention.py`); the tables are created by `AsyncPostgresSaver.setup()`, so
  `tests/test_schema_inventory.py` structurally cannot see them. A library upgrade adding a fourth
  table is invisible to every gate in the repo.
- **A stale docstring reason** (native audit N-10): `agent/langgraph_agent.py:222-227` cites a
  `TypeError` that does not reproduce at these versions.

---

## Vindications — recorded so they are not re-litigated

These were investigated as suspected defects and came back sound. Several are load-bearing.

1. **Durable-job idempotency survived the migration intact.** `job_workflow_id` =
   `f"{connector}-{job}-{stable_hash([connector, job, payload])}"` with
   `ALLOW_DUPLICATE_FAILED_ONLY` (`connectors/jobs.py:254-261,384`); a retry with an identical
   payload rejoins. The deliberate exclusion of `rationale`, `requested_by`, `session_id` and
   `correlation_id` from the key is correct — two chemists with differently-worded reasons must
   rejoin one run. *Caveat, not a defect:* the model is nondeterministic, so a retry may emit a
   different payload and mint a genuinely second run.
2. **The hash chain is not corrupted by retries.** Rows chain and verify correctly under
   `pg_advisory_xact_lock`.
3. **The mid-turn resume does not double-count cost, audit rows, or the repeat-call budget.**
   `turn_usage` is the same object across both passes and the row is written once in `finally`.
4. **`child_workflow_id` including `run_id`** (`durable/connector_job.py:189-214`) is the right fix
   and is what makes a failed template re-executable at all.
5. **`surface_domain_errors` catches `Exception`, not `BaseException`** — so `CancelledError` still
   tears the turn down correctly — and its deliberate *absence* from `invoke_governed` is right,
   because a converted refusal once became a `job` step's payload and launched the workflow it had
   just been denied (`langgraph_agent.py:414-418`).
6. **`HeldConnectorSession` teardown is cancellation-correct** on all ordinary paths. `_shut_down()`
   sets `_stop` before awaiting the holder task, and the holder is parked on `await _stop.wait()`, so
   even a swallowed cancellation lets it unwind its own anyio scope on its own task. Each activity
   attempt opens fresh sessions inside its own `AsyncExitStack`; there is no cross-attempt cache to
   stale out.
7. **All three MAF-keyed modules came through with idempotency intact** — `message_migration.py`
   (versioned stamp, resumable, running it twice converts nothing twice), `message_pairing.py`
   (unreadable ⇒ session undroppable, rather than looking pairing-free), `session_store.py`
   (`message_from_row` is the single shape reader).
8. **`plan_identity` still matches durable rows written under MAF** — it hashes unprefixed `content`,
   and the `[x] ` prefixes are display-only (`graph_stream._todo_titles`).
9. **`failure_exception_types=[Exception]` on `TemplateWorkflow`** (`template_job.py:96`) is correct —
   without it a `ValueError` from `resolve()` suspends the run forever in the SDK's task-failure loop
   (the D-093 trap).
10. Plus the Phase 3 vindication set: stream-mode choice, the refusal to read tool calls from
    `messages`, `_AttributedSpecialist`, `tool_invocation.py`, `repeat_guard`, the durability default,
    and the `BaseStore` decline. See the native audit.

---

## Ranking, and what I would do

Ordered by impact × safety. Everything in tier A is additive and cannot regress a working deployment.

| Rank | Finding | Why here |
|---|---|---|
| **A0** | **T-14 bound the MCP tool call** (`read_timeout_seconds`) | **Top of the list.** A live availability bug reachable with shipped manifest values: one slow tool holds an admission permit for 600 s. The fix converts a hang into an error and is a few lines |
| **A0b** | T-15 make a failed connector tool audit as `error` and raise `ToolFailedEvent` | GxP correctness — the trail currently records a failed call as a success |
| **A1** | T-5 LangSmith pin at every launcher | Default-deny tightening; smallest diff of anything in tier 1; a compliance property |
| **A2** | T-3 set `recursion_limit` explicitly | Additive bound where the current one is inherited |
| **A3** | T-2 meter template agent steps | Additive; closes a "looks free and is not" hole |
| **A4** | T-6 make the conversation window actually bound | ~6 lines, native function, pairing measured safe |
| **A5** | N-1 `UntrackedValue` for the per-turn fields | ~4 lines; **dissolves T-4** and the defect class behind it |
| **A6** | T-9 push back a template failure | Mirrors an existing, tested pattern in `connector_job.py` |
| **B1** | T-13 housekeeping (mcp upper bound + a ratchet on the patched attribute; duplicate migration numbers; checkpoint-table gate; stale docstrings; Phase 1's rename passes) | Mechanical, zero runtime change; F-2's broken dotted path is operator-visible and goes first |
| **C1** | T-1 the plan gate on the template path | **Highest impact of all — and needs your decision.** Touches a GxP control |
| **C2** | T-7 narrow the outer retry policy | Changes failure behaviour under a provider outage |
| **C3** | T-8 idempotency for `run_agent_step` | Re-draws D-002's line; ADR-sized |
| **C4** | T-10 heartbeat + timeouts on template steps | Blocked on a live broker to confirm the failure mode first |

**Tier C is deliberately not "later" — it is "not mine to decide."** Each changes a documented
architectural boundary or a regulatory control, and the brief says behaviour preservation is the
default unless a finding justifies otherwise and you have signed off.

---

## Smaller items from the MCP / concurrency lane

- **`include_detailed_errors` is a ghost setting.** No config field, no reader anywhere in `src/` —
  only comments. Two of them (`agent/tool_authz.py:88`, `api/tool_results.py:67-71`, mirrored in
  `infra/sql/042_tool_result_store.sql:56`) justify a **live data-model decision** — collapsing
  `tool`/`correlation_id` to `''` — on the premise that every unexpected tool exception returns the
  byte string `Error: Function failed.`, which is *MAF's* string and is no longer producible. The
  conclusion survives (the current text is also constant), but `tests/test_tool_results.py:162` pins
  a fixture the system can no longer emit.
- **The in-process lease outlives the durable one by 10×.** In the one window neither `finally`
  covers (response handed off, generator never advanced), the durable claim ages out in 60 s while
  the in-process entry's deadline is `service_turn_timeout_seconds + service_turn_admission_timeout_seconds`
  = **605 s**. A client that vanishes at exactly the wrong moment 409s its own session for ten
  minutes on that pod. The *longer* lease guards the *narrower* failure. Bounded and documented —
  worth a note, not a fix.
- **Specialist attribution is a single variable over a globally-ordered stream**
  (`api/graph_stream.py:116`). Two `task` delegations in one assistant message would interleave as
  `handoff:A, handoff:B, handoff:back, handoff:back`, and everything after the first hand-back would
  be attributed `""` and spliced into the answer. The *audit* stamp is safe (a contextvar, and
  `gather` copies context per task); the stream is not. **Unverified** whether deepagents' `task`
  tool can be invoked twice in one step, and moot today with `agent_teams_enabled=False`.
- **Connector tool names are unprefixed.** `load_mcp_tools` is called without `tool_name_prefix`, so
  connector tools share a namespace with in-process ones and `authorize_tool` keys on the bare name.
  `core/tool_registry.py:49-52` rejects in-process duplicates; nothing checks connector-vs-connector
  or connector-vs-core. Measured on the shipped fleet: 30 declared connector tools, **0 collisions**.
  Latent — it becomes live with a third-party MCP server.

Further vindications from that lane: `surface_domain_errors`'s exception filter is exactly right
(nothing `BaseException`-adjacent is caught; `CancelledError` propagates and `audit.py:290-314` gives
it its own `cancelled` outcome); the four turn guards compose with no bricking window
(both leases are check-and-set with no `await` between test and write, and the durable claim
self-heals via a per-process `_WORKER_ID` that a restarted pod cannot inherit);
`_SlotBoundEventStream` is correct for its stated reason; `absorb_connect_failure`'s
`Task.cancelling() > 0` discriminator is right and measured (cancelling a turn parked on an open
connector propagated cleanly); partial connector failure degrades correctly with
`CapabilityDegradedEvent` before the first token; and `tests/test_agent_team.py` pins both directions
of the token-attribution fix — **22 passed in 4.50 s** when run.

---

## Still open

- The three M12 live probes remain unrun and need a credential. T-10's failure mode specifically
  needs a live Temporal broker, and T-14's fix wants a live re-measurement against a real connector.
- Baseline, measured on this branch before any behavioural change: `make lint` green, `make type`
  green (`mypy --strict`, 628 files), `make prose-validate` green, and `make test`
  **4079 passed, 157 skipped, 0 failed** in 19m15s. Every skip is conditional on a missing service or
  binary — Postgres (27 files), the Temporal test server (13), `xtb`/`crest`/`tblite`, and `make`.
  There is no unconditional `skip` and no `xfail` anywhere in the suite.

  **That number is the point of this document.** 4079 green tests did not see the plan gate missing
  from the template path, the unmetered spend, the audited-as-success connector failure, or the
  unbounded MCP call. Each of those is reachable with shipped defaults.
