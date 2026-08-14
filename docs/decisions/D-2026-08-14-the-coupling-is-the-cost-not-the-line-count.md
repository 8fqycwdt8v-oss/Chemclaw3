# D-2026-08-14-the-coupling-is-the-cost-not-the-line-count — What upstream already does, what it still does not, and the six shapes this repository reads that it was never promised

**Status:** accepted · **Date:** 2026-08-14 · Supersedes the "why not `ModelCallLimitMiddleware`"
paragraph of `agent/loop_cap.py` (D-2026-08-10 phase M1) and the `channel_values` claim in
`agent/plan_state.py`. Amends `D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has` where it
records deepagents' surface, and records that GxP is no longer a constraint on layer 1.

## Context

The instruction was to use what the LangGraph stack offers out of the box as far as it goes, so that
future upstream improvements arrive without a rewrite here. The natural reading of that is "delete
first-party code", and it is the wrong metric.

Layer 1 is ~10.8k lines under `src/chemclaw/agent/`. Almost none of it breaks on a dependency bump,
because almost all of it is policy — an authorization gate, a plan approval, attenuation invariants
— written against upstream's *published* extension points (`wrap_tool_call`, `before_model`,
`wrap_model_call`). What breaks on a bump is the small number of places that read a shape upstream
never promised: a state key's name, a tool's name, a private constant, a baked default, the arity a
hook is invoked with, the tuple width a stream yields. There were six. Each was recorded in the
docstring of whichever module needed it, so a bump could invalidate six sentences in six files and
nothing would go red until a live turn behaved oddly.

So the work split in two: adopt upstream where it genuinely does the job, and — more valuable —
convert internal couplings into declared ones.

Verified against the installed distributions, not the documentation: `langchain 1.3.14`,
`langchain-core 1.5.4`, `langgraph 1.2.10`, `langgraph-checkpoint 4.2.0`, `deepagents 0.7.5`,
`langchain-mcp-adapters 0.3.2`.

## Decision

### 1. The runaway cap is `ModelCallLimitMiddleware`, subclassed only to record that it fired

D-2026-08-10 rejected the upstream middleware and hand-wrote a `before_model` counter. The stated
reason was that upstream keeps `thread_model_call_count` (persisted) and `run_model_call_count` (not),
and "measured against a checkpointed session, the final state carries the thread count and no run
count at all, so *was this turn capped* was unanswerable from it".

That observation is correct and it does not justify a second counter. `run_model_call_count` is
`NotRequired[Annotated[int, UntrackedValue, PrivateStateAttr]]` — the *same* `UntrackedValue`
channel `ChemclawState` had copied from it, plus `OmitFromSchema(input=True, output=True)`, which is
why it is unreadable. What was missing was one boolean, not one counter.

`CappedModelCallLimit` (`agent/loop_cap.py`) subclasses the middleware, delegates the decision to
`super().before_model`, and on the branch that fires writes `loop_capped` and marks the ambient
watch. `ChemclawState.model_calls` is deleted. The two readers are unchanged.

Two things had to be got right and are pinned against a **compiled graph**, because the module's own
history says a unit test on the hook proves nothing here:

- `@hook_config(can_jump_to=["end"])` is re-declared on the override. Without it the cap is inert —
  `_get_can_jump_to` reads the attribute off the subclass's method, and the conditional edge is
  built from that declaration.
- The arithmetic differs (upstream checks in `before_model` and increments in `after_model`; the
  first-party hook did both in `before_model`) and comes to the same number of model calls.
  Measured at caps of 1, 2 and 3: exactly 1, 2 and 3 calls, with both readers reporting the cap.

**The swap has a cost and it was found by a test, not by reading.** `ModelCallLimitMiddleware`
declares `after_model` as well as `before_model` — one more node per iteration. Measured on the real
graph, a harness turn went from 4 supersteps per model call to 5, and the minimal working
`recursion_limit` is now `5N + 3`. `agent_recursion_limit` derived `N * 6 + 1`, which is short by
one at `N = 1` only: a one-iteration harness turn needed 8 and was granted 7, and died with the
`GraphRecursionError` that ceiling exists to *avoid*. The constant is now `+ 3`, and both numbers
carry an instruction to re-measure them together.

### 2. The skills reload is a channel, not a hook override

`ReloadingSkillsMiddleware` exists because `SkillsMiddleware.before_agent` returns early when
`skills_metadata` is already in state, which serves one caller's role-narrowed listing to the next.
The previous fix overrode `before_agent`/`abefore_agent` to hide the key from the state upstream
reads, and had to default a third argument because LangChain invokes the hook with two where
deepagents' own signature declares three. That is a dependency on somebody else's calling
convention — the kind that breaks on a bump without failing loudly, and deepagents' current tree has
already moved to the three-argument form.

The subclass is now one field. `ReloadingSkillsState` redeclares `skills_metadata` as an
`UntrackedValue`, which LangGraph never checkpoints, so the key is absent at the start of every run
and upstream's short-circuit cannot fire. No hook is overridden and the only thing depended on is
the field's name.

Measured over three turns on one thread, recording whether the key was already in state when
`before_agent` ran: `[False, True, True]` on upstream's channel, `[False, False, False]` on this one.
`test_a_role_change_mid_session_renarrows_the_listing` — which already asserted both halves,
including that turn two is not "fixed" by listing nothing — passes unchanged.

This is the same mechanism as §1's `loop_capped` and as `ChemclawState` before it: per-turn is a
property of the channel, never of a caller who remembers to clear it.

### 3. Every unpromised upstream shape is asserted in one file

`tests/test_upstream_surface.py` holds sixteen assertions, each naming the first-party module that
would break. `todos` as the plan channel; `write_todos` as the tool that writes it;
`SubAgentMiddleware._EXCLUDED_STATE_KEYS` still containing `todos` (which is the only reason
`plan_gate._plan_behind`'s session fallback exists); `skills_metadata` as the skills cache key;
`run_model_call_count` still being unreadable (which is the only reason `loop_capped` exists);
`create_agent`'s baked `recursion_limit`; the v3 transformer seam; each deepagents symbol imported
individually because `create_deep_agent` is deliberately not called; and a version floor.

Two of these assert an *absence*, so that upstream fixing something turns the workaround red instead
of letting it outlive its reason:

- `langchain_mcp_adapters` still calls `session.call_tool` with no `read_timeout_seconds`.
- `run_model_call_count` still carries both markers that make it unreadable.

The rule the file states: when one fails, the fix is never to update the number and move on.

### 4. What was proposed, checked, and *not* adopted

Recorded because each looked obviously right until it was read.

- **`ToolErrorMiddleware` in place of `announce_tool_failures`.** It converts a raised exception into
  a `ToolMessage`. `announce_tool_failures` handles both raising *and* returning tools and emits to
  the turn stream. The returned half is the one that matters: `langchain_mcp_adapters` never raises,
  it returns `ToolMessage(status="error")`, so every out-of-process tool failure goes through the
  path upstream's middleware does not see. Not a replacement.
- **`ToolRetryMiddleware` for connector flakiness.** It retries on exceptions only, so for the same
  reason it would never fire on the MCP failures that motivated it. Declined as speculative; if it
  is ever added it needs a read-only allowlist, because retrying a side-effecting tool is not a
  retry.
- **Rewriting `plan_state` off `checkpoint["channel_values"]`.** `channel_values` is a declared field
  of `langgraph.checkpoint.base.Checkpoint`, a public `TypedDict`. Reading it is API use. The
  docstring that listed it beside `todos` as an equally unpromised literal was wrong and is
  corrected; only `todos` needed pinning.
- **A conversation-window `ContextEdit` from upstream.** Still does not exist:
  `context_editing.py` ships `ClearToolUsesEdit` and nothing else, and deepagents'
  `_message_eviction` / `_overflow_clip` offload tool results to a *filesystem backend*, which is
  the write/edit/glob/grep surface D-2026-08-11 declines. `KeepLastConversationGroupsEdit` stands.
- **`SummarizationMiddleware`.** Unchanged: an untrusted summariser persists into history.

`RubricMiddleware` is present in the pinned `deepagents 0.7.5` — no bump is required to reach it,
contrary to the assumption this work started from.

### 5. GxP is no longer a constraint on layer 1

Stated by the product owner this session. It was the justification for the hash-chained audit trail,
the durable plan-approval record, the specialist-attribution wrapper and the `session_messages`
read-model, each of which is now judged only on whether upstream already does the job. The
consequences are scoped in `docs/planning/BACKLOG.md` rather than taken here, for one reason worth
recording: `agent/audit_store.py` is read by `durable/job_record_store.py`,
`durable/audit_chain.py`, `durable/audit_verify.py`, `cli/verify_audit_chain.py`, `core/migrate.py`
and the database-privileges tests, so removing it is a cross-layer change and not an agent-layer one.
Security and identity code — `tool_authz`, `authz`, `reject_widening`, `NarrowedSkillsBackend` — is
**not** affected: it answers to Entra and to the role model, never to GxP.

## Consequences

- `langchain>=1.3.14` replaces `langchain>=1.0`. The old floor was false: `AgentMiddleware.transformers`
  and `InterruptOnConfig.when` (1.3.3) are both load-bearing for work already begun.
- One superstep per model call more, and a ceiling constant that must be re-measured with its
  multiplier. `agent_supersteps_per_model_call` keeps its headroom.
- `ChemclawState` declares one field where it declared two, so the checkpoint stamp
  (`D-2026-08-13-the-guard-must-not-refuse-a-dependency-bump`) now covers `loop_capped` alone.
- A bump becomes one conversation instead of six surprises, which is the whole point of §3.

### 6. The front door stays on `astream`, because v3 cannot book an abandoned turn's tokens

`api/graph_stream.py` drives `graph.astream(stream_mode=_MODES, subgraphs=True)` and unpacks a
3-tuple whose arity had been verified by reading `langgraph.pregel.main._output` — a comment there
warns that writing the mode list as a *tuple* silently changes it. That is the largest single
instance of the coupling this ADR is about, and `stream_events(version="v3")` retires it
structurally: v3 owns `stream_mode` and `subgraphs` and refuses them.

The migration was written and the suite run against it. It is reverted, and the measurement is the
decision.

**v3 reports token usage only at `message-finish`.** Every v3 event was searched for an incremental
signal; there is none. Usage arrives once, aggregated, when a message completes — and again in
`updates`/`values`, later still. `tests/test_turn_cancellation.py` requires that a turn abandoned
*mid-message* books what it has already spent, because otherwise "a user could bypass the token
budget indefinitely by dropping each connection just before the answer, which is the cheapest
possible attack on the runaway-cost guard". Measured under v3: **0 tokens booked**, against ~30 on
the current driver. A coupling that costs maintenance is a smaller harm than a cost guard that can
be walked around, so the coupling stays and `tests/test_upstream_surface.py` is what keeps it from
rotting silently.

Three things learned in the attempt, recorded because they change what a restart looks like:

- **The event contract did not have to change.** The prediction that ordering would move was wrong;
  all sixteen conformance assertions passed unmodified. The migration is repo-local — no
  `Chemclaw3_ui` or `Chemclaw3_mock` change — which removes the reason it looked expensive.
- **The projections cannot be used.** `run.messages` / `run.tool_calls` / `run.subagents` are
  single-consumer cursors, so consuming several concurrently interleaves by whichever cursor is
  pumped, and this module's contract is a global order asserted as a whole sequence. The raw
  protocol stream is ordered and carries a monotonic `seq`.
- **v3's native `tools` method cannot see a specialist's tools.** It carries
  `tool-started`/`tool-finished` whole, which would retire the fragment reassembly D-138 came from,
  but it only sees the tools the *parent* graph's `ToolNode` runs — and `SubAgentMiddleware` invokes
  a compiled specialist as an ordinary runnable inside the `task` tool. Measured: a team turn
  reported the `task` call and nothing the specialist did.

Restart when v3 emits usage per content block, or exposes the raw `(chunk, metadata)` message stream
beside the content-block one. The rest of the work is known to be sound.

## Still open

Three items this pass scoped but did not land — approvals on `HumanInTheLoopMiddleware`,
`RubricMiddleware` in the turn loop, and the audit/transcript collapse — plus the v3 migration above,
are in
`docs/planning/BACKLOG.md` with what was verified about each. Two carry a blocker found by trying
them: `RubricMiddleware`'s revision loop jumps back into the *same* run, so its iterations are
counted by the runaway cap's `run_limit` and the two bounds must be chosen together; and the
transcript cannot leave `session_messages` without a decision about `cli/explain`, which groups a
turn's words with its audit rows on a `correlation_id` the checkpoint does not carry.
