# Verification — slice `agent/` graph core · lens: DOES IT REPRODUCE?

Two findings in scope (1 critical, 1 high). Everything below was re-derived with my own scripts
(`/tmp/rp1.py`, `/tmp/rp2.py`, `/tmp/rp3.py`, `/tmp/rp4.py`); I did not run the reporter's.

---

## LLM failover makes every agent build crash — the AG-12 fallback is unusable

- **Verdict**: CONFIRMED
- **Severity I would assign**: critical
- **What I did**

  Wrote `/tmp/rp1.py` from scratch: set only the four env vars the finding names
  (`CHEMCLAW_LLM_PROVIDER=openai_compatible`, `CHEMCLAW_LLM_BASE_URL`, `CHEMCLAW_LLM_MODEL`,
  `CHEMCLAW_LLM_API_KEY`, plus `CHEMCLAW_LLM_FALLBACK_BASE_URL`), then called `build_chat_model()`
  and `build_langgraph_agent()`.

  ```
  $ uv run python /tmp/rp1.py
  build_chat_model type: <class 'langchain_core.runnables.fallbacks.RunnableWithFallbacks'>
  isinstance BaseChatModel: False
  RAISED: AttributeError 'ChatOpenAI' object has no attribute 'count'
    File "src/chemclaw/agent/langgraph_agent.py", line 251, in build_langgraph_agent
      return create_deep_agent(
    File ".venv/.../deepagents/graph.py", line 604, in create_deep_agent
      model = resolve_model(model)
    File ".venv/.../deepagents/_models.py", line 57, in resolve_model
      return init_chat_model(model, **apply_provider_profile(model))
    File ".venv/.../provider_profiles.py", line 293, in get_provider_profile
      if not spec or spec.count(":") > 1:
    File ".venv/.../langchain_core/runnables/fallbacks.py", line 624, in __getattr__
      attr = getattr(self.runnable, name)
  AttributeError: 'ChatOpenAI' object has no attribute 'count'
  ```

  Control, `/tmp/rp2.py`, identical config with `CHEMCLAW_LLM_FALLBACK_BASE_URL` unset:

  ```
  no-fallback type: <class 'langchain_openai.chat_models.base.ChatOpenAI'>
  no-fallback compiled ok: <class 'langgraph.graph.state.CompiledStateGraph'>
  ```

  Line numbers and symbols checked against the current tree: `llm_provider.py:59` is
  `return _with_failover(primary, model)`; `:86` is `primary.with_fallbacks(...)`;
  `langgraph_agent.py:251` is `return create_deep_agent(`. `deepagents/_models.py:36-57` is
  `resolve_model`, whose only escape is `isinstance(model, BaseChatModel)` — which I printed as
  `False` for the returned object.

  Coverage gap re-checked rather than taken on faith: `grep -rln "llm_fallback" tests/` returns
  exactly one module, `tests/test_llm_provider.py`, and `grep -rn "build_langgraph_agent"
  tests/test_llm_provider.py` returns nothing. So no test in the suite ever hands a failover-wrapped
  model to a builder.

- **Why**

  It reproduces on the first try, from config alone, with no scaffolding of the reporter's. The
  trigger is a plain settings value (`core/config/llm.py:68`, `llm_fallback_base_url: str = ""`)
  whose only effect is to switch this on, and the failure is at *construction*, before any network
  call — so it is not a degraded answer, it is `build_langgraph_agent()` raising on every profile,
  every CLI turn, every front-door turn and every template step.

  Two things I checked that make it neither better nor worse than stated, and worth recording so
  triage does not re-derive them:

  - The Anthropic path is genuinely unaffected — `_with_failover` is only reached from the
    `openai_compatible` branch, and `_anthropic_model` returns a real `ChatAnthropic`.
  - The `helper=True` branch (`langgraph_agent.py:248-250`, `create_agent(**shared)`) *tolerates*
    the `RunnableWithFallbacks` — it is built as an argument to `create_deep_agent` and completed
    before the crash. So the defect is specifically `create_deep_agent`'s `resolve_model`, which is
    the fix constraint: any repair must produce something `isinstance(..., BaseChatModel)`.

  The reporter's proposed regression test (`build_langgraph_agent(model=build_chat_model())` with
  the fallback configured) is the right one — the existing wrapper-shape assertions cannot see this
  class of defect, as the grep above shows.

---

## A loop-capped turn hands the chemist a raw tool result as the answer

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium
- **What I did**

  Wrote `/tmp/rp3.py`: `CHEMCLAW_HARNESS_ENABLED=true`, `CHEMCLAW_HARNESS_MAX_LOOP_ITERATIONS=3`,
  a `GenericFakeChatModel` subclass whose `_generate` always emits a `write_todos` tool call, then
  `build_langgraph_agent(model=...)`, `ainvoke`, and `chemclaw.cli.chat._answer_text` on the result.

  ```
  $ uv run python /tmp/rp3.py
  the model loop hit its 3-iteration cap
  n messages: 7
  model_calls: 3 loop_capped: True
  last msg type: ToolMessage
  ANSWER TEXT => "Updated todo list to [{'content': 'x', 'status': 'pending'}]"
     HumanMessage 'hi'
     AIMessage '' ['write_todos']
     ToolMessage "Updated todo list to [{'content': 'x', 'status': 'pending'}]"
     AIMessage '' ['write_todos']
     ToolMessage "Updated todo list to [{'content': 'x', 'status': 'pending'}]"
     AIMessage '' ['write_todos']
     ToolMessage "Updated todo list to [{'content': 'x', 'status': 'pending'}]"
  ```

  Identical to the reporter's numbers — exactly `cap` model calls, `loop_capped=True`, last message
  a `ToolMessage`, and `_answer_text` returning its content verbatim. The structural claim also
  holds on inspection: `enforce_loop_cap` (`loop_cap.py:144-155`) cannot fire on the first
  `before_model` (`harness_max_loop_iterations` is `Field(ge=1)`, so `0 >= cap` is never true), so
  the jump to `end` is always taken from the `before_model` that follows a tool node — i.e. a capped
  turn's last message is *always* a `ToolMessage`. Not "usually".

  Where I part company with the finding is reach. I checked the three surfaces that call a graph:

  - `src/chemclaw/cli/chat.py:136` — affected, exactly as reported.
  - `src/chemclaw/api/runner.py:368` — not affected, as the finding itself concedes: the answer is
    accumulated from `TokenEvent`s, not from `messages[-1]`, and `loop_hit_cap()` yields an explicit
    `loop_cap_reached` `ErrorEvent` before it.
  - `src/chemclaw/durable/template_activities.py:409` — has its own byte-identical `_answer_text`
    (`:414-431`), which the finding does not mention. But `step_profile` runs a template step with
    the harness **off**, so `_harness_middleware` returns `[]` and `enforce_loop_cap` is never
    attached; the cap cannot fire there. Not affected.

  And on who is actually exposed: `grep -rn "command:" deploy/helm/chemclaw/templates/*.yaml`
  shows the shipped workloads are the API service, the migrate job and
  `python -m chemclaw.cli.schedules`. Nothing deploys `chemclaw chat`. The chemist's surface is the
  front door, which is covered; the CLI is a local/operator path.

- **Why**

  The mechanism, the trigger and the code path are all exactly as described and reproduce on my own
  script with matching numbers — nothing here is fabricated. What does not hold is the framing that
  gives it "high": the title's *chemist* does not reach this code. `CHEMCLAW_HARNESS_ENABLED: "true"`
  in `deploy/helm/chemclaw/values.yaml:339` makes the cap live in production, but production runs
  the SSE runner, which is the one surface that reads the cap correctly. To be bitten you need an
  operator at a terminal running `make chat` with the harness on *and* a turn that burns 25
  iterations. That is a real, silently-wrong output on a secondary surface — medium — not a
  fleet-wide product failure.

  The proposed fix is still right and cheap (walk back to the last non-empty `AIMessage`; prefix
  with the partial-answer notice when `loop_capped(result)`), and the reporter is correct that no
  new plumbing is needed — I read `loop_capped: True` straight off the returned state above.

  One caveat on the secondary path the reporter flagged but did not claim (`SubAgentMiddleware`
  reporting an empty string for a capped helper): I could not settle it. It is *plausible* —
  `deepagents/middleware/subagents.py:495-505` walks back to the last non-empty `AIMessage` and
  falls through to `content = ""`, and `_validate_and_prepare_state` (`:534-539`) copies
  non-private state into the helper, so the parent's `model_calls` does cross over. My attempt
  (`/tmp/rp4.py`) failed on my own fixture, not on the code — my helper-detection predicate looked
  at `messages[0]`, which is the system prompt, so the helper's fake model emitted a `task` call the
  helper graph correctly has no tool for. `task` *is* registered on the parent under a
  harness-enabled build (verified: `'task' in tools_by_name` → `True`, 36 tools). Treat that path as
  open, not as refuted.
