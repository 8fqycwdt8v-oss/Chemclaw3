# D-039 — F0: config-selected LLM provider seam (foundation-plan D-A1)

**Context.** The target deployment serves the LLM from an internal OpenAI-compatible ("OpenLLM-like")
endpoint, not Anthropic. The agent must reach it by config, and the raw inference credential is
**one generic API key, not per-user Entra** (the model call is not a user-scoped resource; identity
scoping applies to *who* takes the turn / *which* workflow runs, handled in F4).

- **One import site.** `agents/llm_provider.py::build_chat_client()` is the only place a chat-client
  class is imported (mirrors the ELN adapter registry). `build_agent` calls it; the deleted
  `_default_chat_client` is gone. `settings.llm_provider ∈ {openai_compatible, anthropic}`.
- **openai_compatible** builds MAF `OpenAIChatClient(model=llm_model, async_client=AsyncOpenAI(...))`,
  where the `AsyncOpenAI` carries `llm_base_url`, the generic `llm_api_key` (a non-empty placeholder
  if the endpoint is keyless), `llm_timeout_seconds`, `llm_max_retries`, and a CA-pinned httpx client
  when `llm_tls_ca_bundle` is set — so a firewalled internal endpoint with a private CA works from
  config alone. **anthropic** keeps the pre-seam dev path (its own key preflight, `agent_model`).
- **Default `anthropic`** so the config singleton is valid with no endpoint set; production sets
  `CHEMCLAW_LLM_PROVIDER=openai_compatible` + base_url/model (validated at startup).
- **Generation params** (`llm_temperature`/`llm_max_tokens`) thread onto `Agent(default_options=…)`.
- New dep: `agent-framework-openai`. Tests: `test_llm_provider`, `test_config`, `test_agent`.
- **Open (F0-T4):** the internal model's function-calling reliability is the project's #1 risk; a
  spike verdict (`docs/spikes/f0-toolcalling.md`) is pending a live endpoint before building further.
