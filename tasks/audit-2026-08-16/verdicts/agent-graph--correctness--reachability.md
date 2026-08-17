# Verdicts — slice `agent/` graph core · lens: reachability & consequence

Scope: the two findings marked **critical**/**high**. The four `low`/`medium` rows are out of scope
and were not examined.

Working tree checked against the pristine `HEAD` copy first — `llm_provider.py`, `loop_cap.py`,
`cli/chat.py` and `langgraph_agent.py` are all byte-identical to it, so nothing below is an artefact
of another agent's mutation experiment.

---

## LLM failover makes every agent build crash — the AG-12 fallback is unusable

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (the finding says critical; one notch down, reasons below)
- **What I did**

  Reproduced the whole path from config to `build_langgraph_agent()`, with nothing injected —
  `/tmp/v1.py`, env only:

  ```
  CHEMCLAW_LLM_PROVIDER=openai_compatible CHEMCLAW_LLM_BASE_URL=http://a/v1
  CHEMCLAW_LLM_MODEL=prim CHEMCLAW_LLM_API_KEY=primary-key
  CHEMCLAW_LLM_FALLBACK_BASE_URL=http://b/v1 CHEMCLAW_LLM_FALLBACK_API_KEY=standby-key
  ```
  ```
  bound type: <class 'langchain_core.runnables.fallbacks.RunnableWithFallbacks'>
  Traceback (most recent call last):
    File "/tmp/v1.py", line 13, in <module>
      a = build_langgraph_agent()
    File ".../chemclaw/agent/langgraph_agent.py", line 251, in build_langgraph_agent
      return create_deep_agent(
    File ".../deepagents/graph.py", line 604, in create_deep_agent
      model = resolve_model(model)
    File ".../deepagents/_models.py", line 57, in resolve_model
      return init_chat_model(model, **apply_provider_profile(model))
    File ".../provider_profiles.py", line 293, in get_provider_profile
      if not spec or spec.count(":") > 1:
  AttributeError: 'ChatOpenAI' object has no attribute 'count'
  ```

  Traced the reachability chain outward and found nothing in the way:
  - `src/chemclaw/core/config/llm.py:68-70` — `llm_fallback_base_url: str = ""`, a plain string
    field with no validator, no cross-field check, no startup guard.
  - `.env.example:563-571` documents the setting and *recommends* it: "the one gap whose failure is
    total rather than degraded ... one outage fails every turn for the whole fleet". So the operator
    action that triggers this is the action the shipped documentation asks for.
  - `deploy/helm/chemclaw/values.yaml:336-337` puts the production deployment on exactly the
    affected branch (`CHEMCLAW_LLM_PROVIDER: openai_compatible`). The chart does **not** set the
    fallback key, so nothing shipped is broken today — enabling it is a one-line ConfigMap edit.
  - `src/chemclaw/agent/langgraph_agent.py:216` takes `build_chat_model()` whenever `model is None`,
    which is every production caller: `api/runner.py:277` (`graph_factory=build_langgraph_agent`,
    per turn), `cli/chat.py:_build_cli_agent`, `durable/template_activities.py:392` (every template
    step). Only tests inject a model, which is why the suite is blind to it.

  Confirmed the fault is `create_deep_agent`-specific rather than a LangChain-wide rule, which
  matters for the fix (`/tmp/v3.py`): `create_agent(model=<RunnableWithFallbacks>, tools=[])` →
  `create_agent OK: <class 'langgraph.graph.state.CompiledStateGraph'>`. So the helper branch
  (`langgraph_agent.py:249`) would have survived; the main branch is what dies.

  Confirmed the "no test hands the wrapper to the builder" claim by reading
  `tests/test_llm_provider.py:192-289`: all five fallback tests assert on the object
  `build_chat_model()` returns (`type(...).__name__`, `.fallbacks[0]`, `.exceptions_to_handle`,
  `.bind_tools`). None calls `build_langgraph_agent`.

- **Why**

  Trigger reachable, mechanism exact, consequence essentially as stated. The one correction on
  consequence: the process does not die and no 500 escapes. At the front door the `AttributeError`
  is raised inside `run_turn`'s body and caught at `api/runner.py:503`, which logs it and emits one
  `ErrorEvent("The turn could not be completed due to an internal error …")` with the correlation
  id. That is still *every turn failing* — the finding's substantive claim — but the failure mode is
  "the whole fleet answers 'internal error'", not a crash-loop. The CLI and template-step paths do
  propagate the raw `AttributeError`.

  I take the severity down one notch to **high**, not because any part of the finding is wrong but
  because of where the failure lands in time: no shipped configuration triggers it, it fires at
  graph *construction* on the very first turn after the config change, and it is 100 % deterministic
  — a deployment that enables the fallback discovers it in the first smoke request rather than
  months later on a wrong answer. Critical, in this audit, should be reserved for what a live
  deployment is exposed to now or what fails silently. This is neither. Everything else about the
  finding — including the fix direction and the demand that the regression test be
  `build_langgraph_agent(model=build_chat_model())` — I would act on unchanged.

---

## A loop-capped turn hands the chemist a raw tool result as the answer

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium (the finding says high)
- **What I did**

  Reproduced end-to-end through the CLI's own `_answer_text` (`/tmp/v2.py`: real
  `build_langgraph_agent`, `harness_enabled=True`, `harness_max_loop_iterations=3`, a scripted model
  that calls `write_todos` forever, `InMemorySaver`):

  ```
  2026-08-17 05:47:53,416 WARNING chemclaw.agent.loop_cap: the model loop hit its 3-iteration cap
  loop_capped: True
  last msg type: ToolMessage
  CLI would print: "Updated todo list to [{'content': 'x', 'status': 'pending'}]"
     HumanMessage 'go'
     AIMessage ''
     ToolMessage "Updated todo list to [{'content': 'x', 'status': 'pending'}
  --- what the CLI prints on stdout:
  Updated todo list to [{'content': 'x', 'status': 'pending'}]
  ```

  Then traced how far the blast radius actually reaches:
  - **Cap attachment.** `langgraph_agent.py:452` — `enforce_loop_cap` is attached only when
    `harness_enabled_for(profile)` (`agent/plan_gate.py:133`). Default is `False`
    (`core/config/agent.py:141`, `.env.example:632`), but `deploy/helm/chemclaw/values.yaml:339`
    sets `CHEMCLAW_HARNESS_ENABLED: "true"`, so the cap **is** live in the shipped deployment. That
    half of reachability holds.
  - **Second consumer of the same pattern.** `durable/template_activities.py:414` has a byte-for-byte
    copy of `_answer_text` reading `messages[-1].content`, and it feeds a template step's output into
    the report pipeline — a worse surface than the CLI. It is **not** reachable: `step_profile`
    (`template_activities.py:473`) overrides `harness_enabled=False`, so the loop cap is never
    attached on that path, and its own comment at line 385 says so ("this path runs with the harness
    off, so the loop cap is not attached"). Verified by reading `model_copy(update={...})`. So the
    exposure is genuinely the CLI alone.
  - **"No marker that the turn was cut short" — this is the part that does not hold.**
    `loop_cap.py:146` logs `logger.warning("the model loop hit its %d-iteration cap", calls)`, and
    the CLI calls `configure_logging()` at `cli/chat.py:332`, whose default level is `INFO`
    (`core/config/observability.py:23`). Run under that configuration the warning above is what
    stderr carries. A person at a terminal is told the cap fired, on the same screen, immediately
    before the answer. The claim survives only for a redirected `--message` run, where stdout
    carries the tool text alone.

- **Why**

  The mechanism is real and I would still fix `_answer_text` — a `ToolMessage` body printed where the
  assistant's prose belongs is wrong on its face, and `loop_capped(result)` is right there in the
  returned state. But three things pull it below "high":

  1. **The chemist-facing surface is already correct.** `api/runner.py:365-385` emits
     `ErrorEvent(code="loop_cap_reached", …"the answer below is partial")` before the answer, and the
     UI consumes that stream. The affected surface is `chemclaw chat`, an operator/dev entry point.
  2. **The interactive case is marked**, by the WARNING line above; only the piped case is silent.
  3. **What is shown is genuine tool output, not fabricated content.** Nothing invents a number.
     Against the audit's safety rule: the worst realistic form here is a chemist seeing a safety or
     property tool's *raw result payload* labelled as the answer instead of the model's reading of
     it — misframed and missing the partial-turn caveat, but not a false value and not a suppressed
     hazard. The paraphrase "hands the chemist a raw tool result as the answer" is accurate; the
     implied severity of it is not, because the tool result is true and the accompanying stderr line
     says the turn stopped early.

  The finding's own comparison also cuts slightly the other way from how it is written: upstream's
  `exit_behavior="end"` string would be *printed instead of* the partial answer and checkpointed into
  the thread forever, whereas this leaves the real messages intact and merely picks the wrong one to
  render — a rendering bug in one CLI function, not a state defect.

  The second path the finding flags without reproducing (a capped `SubAgentMiddleware` helper
  reporting an empty last `AIMessage`) I did not attempt; it is unproven either way and would need
  its own repro against a spawned `task`.
