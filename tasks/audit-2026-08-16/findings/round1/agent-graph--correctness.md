# Round 1 — slice: `agent/` graph core · lens: CORRECTNESS

Files read in full: `langgraph_agent.py`, `chemclaw_agent.py`, `checkpointer.py`, `compaction.py`,
`loop_cap.py`, `graph_tools.py`, `llm_provider.py`.

Everything below was reproduced by running code in this checkout (`uv run`, scripts under `/tmp`).
Two mechanisms I expected to be wrong turned out to be sound and are recorded at the bottom so the
triage step does not re-spend time on them.

---

## LLM failover makes every agent build crash — the AG-12 fallback is unusable

- **Severity**: critical
- **Location**: `src/chemclaw/agent/llm_provider.py:59` (`build_chat_model` → `_with_failover`), consumed at `src/chemclaw/agent/langgraph_agent.py:251` (`create_deep_agent(**shared)`)
- **Trigger**: any deployment on the documented production path with the second endpoint configured —
  `CHEMCLAW_LLM_PROVIDER=openai_compatible`, `CHEMCLAW_LLM_BASE_URL=…`, `CHEMCLAW_LLM_MODEL=…`,
  **and** `CHEMCLAW_LLM_FALLBACK_BASE_URL=…`. Then `build_langgraph_agent()` (i.e. every turn:
  `api/runner.py:282`, `cli/chat.py`, every template step).
- **Consequence**: `AttributeError: 'ChatOpenAI' object has no attribute 'count'` raised during graph
  construction. Not a degraded answer — **no turn can be taken at all**, fleet-wide, the moment the
  failover setting is filled in. The feature whose config comment says it is the one gap "whose
  failure is total rather than degraded" is itself total failure when switched on.
- **Evidence**: `_with_failover` returns `primary.with_fallbacks(...)`, i.e. a
  `RunnableWithFallbacks`, which is **not** a `BaseChatModel`. `create_deep_agent` opens with
  `model = resolve_model(model)` (`deepagents/graph.py:604`), and
  `deepagents/_models.py:56` is `if isinstance(model, BaseChatModel): return model` — otherwise the
  argument is treated as a *model spec string* and reaches
  `provider_profiles.py:293`, `if not spec or spec.count(":") > 1:`.
  `RunnableWithFallbacks.__getattr__` forwards `count` to the wrapped `ChatOpenAI`, which has no
  such attribute.

  Reproduced (`/tmp/t1.py`):
  ```
  bound type: <class 'langchain_core.runnables.fallbacks.RunnableWithFallbacks'>
  ...
    File ".../deepagents/graph.py", line 604, in create_deep_agent
      model = resolve_model(model)
    File ".../provider_profiles.py", line 293, in get_provider_profile
      if not spec or spec.count(":") > 1:
  AttributeError: 'ChatOpenAI' object has no attribute 'count'
  ```
  Control (`/tmp/t2.py`), same config with `llm_fallback_base_url` unset:
  ```
  no-fallback type: <class 'langchain_openai.chat_models.base.ChatOpenAI'>
  no-fallback compiled ok
  ```
  Why no test caught it: `tests/test_llm_provider.py:194-289` asserts five properties of the object
  `build_chat_model()` returns (wrapper type, fallback model/key, handled exception set, `bind_tools`
  survival) and never hands that object to `build_langgraph_agent`. The docstring at
  `llm_provider.py:77-82` measured `bind_tools`, which is downstream of the call that actually
  fails.
- **Fix**: the fallback has to be applied *below* `create_deep_agent`'s model resolution, not above
  it. Either (a) build the failover as a `BaseChatModel` subclass/`configurable_alternatives`
  arrangement, or (b) keep `build_chat_model` returning the bare primary and attach the fallback
  inside a `wrap_model_call` middleware that catches `_failover_exceptions()` and re-issues against
  the standby model, or (c) at minimum call `resolve_model`-equivalent yourself and pass a
  `BaseChatModel`. Whichever is chosen, the regression test must be
  `build_langgraph_agent(model=build_chat_model())` with the fallback configured — the unit
  assertions on the wrapper cannot see this class of defect.

---

## A loop-capped turn hands the chemist a raw tool result as the answer

- **Severity**: high
- **Location**: `src/chemclaw/agent/loop_cap.py:154` (`enforce_loop_cap` returns `{"jump_to": "end", …}`), surfaced at `src/chemclaw/cli/chat.py:144` (`_answer_text`)
- **Trigger**: `harness_enabled=True`, any turn that reaches `harness_max_loop_iterations`. Because
  `enforce_loop_cap` is a `before_model` hook, the jump to `end` always happens *immediately after a
  tool node*, so the thread's last message is always a `ToolMessage`.
- **Consequence**: `cli/chat.py:_answer_text` returns `messages[-1].content` — the raw tool output —
  as the assistant's reply, with no marker that the turn was cut short. Reproduced output was
  `"Updated todo list to [{'content': 'x', 'status': 'pending'}]"` presented as the answer. This is
  the same failure mode `loop_cap.py:112-119` rejects `ModelCallLimitMiddleware` for
  ("`cli/chat.py` prints `messages[-1].content`, so a capped CLI turn printed the limit string
  *instead of* the partial answer") — the replacement did not avoid it, it changed *which* wrong
  string gets printed, and the current one is worse because a tool result reads like content rather
  than like a system notice. `api/runner.py:368` does emit `loop_cap_reached`, so the SSE front door
  is covered; the CLI is not.
- **Evidence**: `/tmp/t3b.py`, cap 3, a model that always calls `write_todos`:
  ```
  the model loop hit its 3-iteration cap
  model calls: 3 cap: 3
  loop_capped: True
  model_calls state: 3
  last msg type: ToolMessage "Updated todo list to [{'content': 'x', 'status': 'pending'}]"
  ```
  (The count itself is correct — exactly `cap` model calls, no off-by-one.)
  A second path with the same root cause, which I did **not** reproduce and flag only so triage
  knows to check it: `SubAgentMiddleware` builds a helper's report from the last non-empty
  `AIMessage`; a capped helper's last `AIMessage` is the empty-content tool-call turn, so the report
  is empty.
- **Fix**: `_answer_text` must not assume `messages[-1]` is the assistant's prose. Walk back to the
  last `AIMessage` with non-empty content, and when `loop_capped(result)` is true prefix the CLI
  output with the same partial-answer notice `api/runner.py` emits. The state already carries the
  fact (`loop_capped` is in the returned state — verified above), so no new plumbing is needed.

---

## `llm_fallback_model` is silently ignored whenever `model_routes` names the task

- **Severity**: medium
- **Location**: `src/chemclaw/agent/llm_provider.py:250` (`_openai_compatible_model`)
- **Trigger**: `model_routes={"agent": "routed-large"}` (per-task routing, F10-E) together with
  `llm_fallback_base_url` + `llm_fallback_model="standby-model"`.
- **Consequence**: the standby endpoint is asked for `routed-large`, the *primary's* routed model.
  If the standby is a different vendor/deployment (the exact case `llm_fallback_model` exists for),
  every failover request comes back `404 model_not_found` — so the failover fires during an outage
  and then fails, which is indistinguishable from having no failover at all. The inconsistency is
  the tell: `llm_fallback_api_key` **is** honoured in the same expression, so the standby gets its
  own credential and the wrong model name.
- **Evidence**: `chosen = model or (settings.llm_fallback_model if fallback else "") or settings.llm_model`
  — `model` is `settings.model_routes.get(task)`, evaluated before the fallback branch, so a
  non-`None` route short-circuits it. Reproduced (`/tmp/t4.py`):
  ```
  primary model: routed-large key: primary-key
  fallback model: routed-large key: standby-key url: http://b/v1
  ```
  `tests/test_llm_provider.py:229` ("the fallback may name its own model") passes only because
  `model_routes` is empty in that fixture.
- **Fix**: make the precedence explicit per side —
  `chosen = (settings.llm_fallback_model or model or settings.llm_model) if fallback else (model or settings.llm_model)`
  — or introduce `model_routes_fallback`. Add the `model_routes`-set case to the existing test.

---

## `prompt_caching_middleware`'s "upstream composes nothing there either" is false

- **Severity**: low
- **Location**: `src/chemclaw/agent/llm_provider.py:196-197` and `:204-205` (`prompt_caching_middleware`)
- **Trigger**: `llm_provider=openai_compatible` (the documented production target). The function
  returns `[]` on the first gate.
- **Consequence**: the docstring's claim — "The empty list is still right when the provider is not
  Anthropic: upstream composes nothing there either, so there is no slot to occupy and no
  `langchain_anthropic` import to make", and with it the stated *structural* guarantee that a
  non-Anthropic deployment never carries Anthropic-specific middleware — does not hold.
  `deepagents/middleware/_prompt_caching.py:41` appends `AnthropicPromptCachingMiddleware`
  **unconditionally**, with no model check, and imports `langchain_anthropic` at module scope. It is
  benign at runtime today only because it is constructed with `unsupported_model_behavior="ignore"`
  and `_should_apply_caching` starts with `isinstance(request.model, ChatAnthropic)` — i.e. the
  safety comes from an upstream default, which is precisely what this seam's own rule
  ("a decision belonging to `settings` may not be made by an upstream default, in either direction")
  forbids relying on.
- **Evidence**: `/tmp/t8.py`, spying on `deepagents.graph._apply_custom_middleware`'s return value
  with `llm_provider=openai_compatible`:
  ```
  chemclaw contributes: []
  final stack: [... 'ContextEditingMiddleware', 'RecordContextCompaction', 'AnthropicPromptCachingMiddleware']
  anthropic caching present: True
  ```
  Same run also shows the *declared* order in `_middleware` (caching before compaction) is inverted
  in the real stack, because a name-replacement keeps upstream's tail position while new entries
  land before the tail — so `langgraph_agent.py:322-324`'s "Last, so the reduction sees everything
  the middleware above it added" is not what is built either. No behavioural consequence found
  (the breakpoints are on system prompt/tools, which compaction does not touch).
- **Fix**: return `[_CachingDisabled()]` on every non-Anthropic provider too, so the slot is
  occupied by decision rather than left to upstream's default; and correct both docstrings.

---

## The checkpointer's schema stamp guards only channels LangGraph never restores

- **Severity**: low
- **Location**: `src/chemclaw/agent/checkpointer.py:211` (`FIRST_PARTY_CHANNELS`), `:249` (`SchemaStampedSaver.aget_tuple`)
- **Trigger**: today's `ChemclawState`. `FIRST_PARTY_CHANNELS` computes to exactly
  `('loop_capped', 'model_calls')` — and both are declared
  `NotRequired[Annotated[…, UntrackedValue]]` in `agent/state.py`, i.e. channels the checkpointer
  **never writes** and never restores.
- **Consequence**: the failure the whole module is built around — "a node indexes a channel the
  checkpoint never held and raises a bare `KeyError`" — cannot be produced by either channel this
  guard currently stamps, since neither is ever restored from a checkpoint on any build, and
  `enforce_loop_cap` reads its one with `state.get("model_calls", 0)` anyway. What remains live is
  the *over-refusal* the module docstring concedes: renaming or adding a first-party channel refuses
  every in-flight thread (`CheckpointSchemaMismatch` → `internal`, non-retryable turn failure) for a
  crash that could not have happened. So the guard's only reachable effect today is the harm, not
  the protection. It becomes genuinely useful the moment a *checkpointed* (`LastValue`) first-party
  channel is declared — which is worth writing down, because nothing in the code says so.
- **Evidence**:
  ```
  $ uv run python -c "from chemclaw.agent.checkpointer import FIRST_PARTY_CHANNELS; print(FIRST_PARTY_CHANNELS)"
  ('loop_capped', 'model_calls')
  ```
  against `agent/state.py:` `model_calls: NotRequired[Annotated[int, UntrackedValue]]` and
  `loop_capped: NotRequired[Annotated[bool, UntrackedValue]]`.
- **Fix**: derive the stamp from the channels that are actually checkpointed — subtract those whose
  annotation carries `UntrackedValue` — so the stamp is over the set where a restore can fail. That
  removes both the false protection and the false refusal in one change.

---

## `state.py` documents a `loop_cap` mechanism that does not exist

- **Severity**: low
- **Location**: `src/chemclaw/agent/state.py:51` and `:85` (describing `src/chemclaw/agent/loop_cap.py`)
- **Trigger**: reading either file to find out how the runaway cap works.
- **Consequence**: line 51 says "since M14 … `agent/loop_cap.py` subclasses that middleware rather
  than counting again, and the one field left here is the record", and line 85 says
  "`loop_cap.CappedModelCallLimit.before_model` writes this". Neither is true: `loop_cap.py` defines
  no class at all — the cap is the `@before_model(can_jump_to=["end"])` function `enforce_loop_cap`,
  which *does* count (`model_calls`), and `ModelCallLimitMiddleware` is not imported anywhere in
  `src/`. `state.py` also still declares `model_calls`, contradicting "the one field left here".
  A reader following `state.py` looks for `CappedModelCallLimit`, does not find it, and the two files
  disagree about which of them owns the counter — the exact drift `loop_cap.py`'s own docstring was
  written to prevent.
- **Evidence**: `grep -rn "ModelCallLimitMiddleware\|CappedModelCallLimit" src/` returns only prose
  in `loop_cap.py`'s and `state.py`'s docstrings; `loop_cap.py:90-155` is the whole mechanism.
- **Fix**: rewrite both comments to describe the `before_model` hook. (`loop_cap.py`'s own docstring
  is already correct and explains why the delegation was reverted — `state.py` was not updated with
  it.)

---

## `find_notes` logs a truncation warning when nothing was truncated

- **Severity**: low
- **Location**: `src/chemclaw/agent/graph_tools.py:115-122`
- **Trigger**: a query matching exactly `settings.graph_max_results` (default 50) current notes.
- **Consequence**: the cap check runs *after* appending, so the `cap`-th match trips
  `len(matches) == cap` and logs `find_notes capped at 50 matches (id order) for …; narrow the query
  or raise CHEMCLAW_GRAPH_MAX_RESULTS` even though the result set is complete. No data is lost — the
  break is correct — but the operator signal that D-066 #4 asked for ("never a silent cap") fires on
  a non-event, which is how an operator learns to ignore it.
- **Evidence**: the loop body appends, then compares, then breaks; there is no lookahead over the
  remaining nodes.
- **Fix**: check `len(matches) > cap` after appending an extra candidate and drop it, or test the
  cap before the append and set a `truncated` flag only when the iteration would have continued.

---

## Checked and found sound (do not re-spend budget here)

- **`enforce_loop_cap` arithmetic.** Cap N permits exactly N model calls, `loop_capped` is `True`
  only on the branch that stops the loop, and both fields reset per turn because they are
  `UntrackedValue`. Measured at cap 3: 3 model calls, `loop_capped=True`, `model_calls=3`.
- **`KeepLastConversationGroupsEdit`.** The "kept is a suffix" assumption holds
  (`trim_messages(strategy="last", allow_partial=False)` can only drop a prefix), the cut always
  lands on a `HumanMessage` index, and the `starts[-1]` clamp prevents an empty list. Driven
  end-to-end at an 80-message thread with a 500-token budget: 80 → 8 messages, first survivor a
  `HumanMessage`, system prompt intact (`['SystemMessage','HumanMessage','AIMessage',…]`).
- **`disabled_summarizer(trigger=None)`.** Verified against the installed
  `SummarizationMiddleware`: `_should_summarize` opens with `if not self._trigger_clauses: return
  False`, and `None` normalizes to no clauses. The summarizer genuinely cannot fire.
- **`_record_reduction` double-count.** `ContextEditingMiddleware` deep-copies the request's message
  list even when no edit applies, so a call needing no reduction still compares equal-token lists
  and the `reclaimed <= 0` guard suppresses the tick.
- **`skill_permits` staleness.** It returns a closure, not a materialized set, so the role gate is
  genuinely evaluated per reach as claimed.
- **Middleware name collisions.** No two entries in `_middleware`'s output share a `.name`, so
  `_apply_custom_middleware`'s name-keyed replacement dict cannot silently drop one.
- **`_first_party_channels` base subtraction.** `ChemclawState.__orig_bases__` is populated on 3.11
  and the subtraction correctly yields only the two first-party names.
