# One gateway, no providers — 2026-09-04

**The ask:** stop using the `anthropic` SDK directly; reach every model through one
centrally-managed, OpenAI-compatible gateway, so any vendor behind it is the gateway's
business rather than this codebase's.

**Owner decisions taken:**
1. Accept the loss of Anthropic prompt caching; record the cost in the ADR.
2. **Remove the provider concept entirely** — no `llm_provider` field, not a one-value enum.

## Why this is urgent independently of the redesign

`llm_base_url` is **silently ignored** on the anthropic path, and the guard meant to catch
that believes the opposite. Measured:

    llm_provider="anthropic", llm_base_url="https://gateway.internal/v1"
      -> build_chat_model("agent").anthropic_api_url = 'https://api.anthropic.com'

`api/middleware.py::_refuse_public_llm_exposure` returns early whenever `llm_base_url` is
truthy, and its docstring claims an `llm_base_url` "naming an anthropic-compatible gateway"
satisfies it. It does not. So a network-exposed pod configured with an internal gateway URL
boots clean, is allowed `api.anthropic.com` by `netguard.py:188`, and sends every prompt and
completion to the public API. Nothing tests that combination.

Removing the provider concept makes this **unrepresentable** rather than fixed, which is why
the two are one change.

Two more silent gaps on the same path — which is the shipped *default*:
- `llm_fallback_base_url` is never applied (`build_chat_model:97` skips `_with_failover`),
  on the provider whose config comment calls failover "the one gap whose failure is total".
- `evals/live_judge.py` posts the **Anthropic protocol** to a doubled `/v1/v1/messages` path
  against a gateway, degrading every probe to `ungraded` rather than erroring.

## The work

- [ ] **1. Delete the provider concept.** `llm_provider` gone from `core/config/llm.py` and
      from all nine readers. With it: `_anthropic_model`, `_require_anthropic_key`,
      `_CachingDisabled`, the Anthropic arm of `prompt_caching_middleware`,
      `_effort_is_provider_scoped`, `build_chat_model`'s effort guard, `llm_prompt_caching`,
      `_refuse_public_llm_exposure`, the `netguard` branch, and `agent_model` (vestigial —
      its only readers are `_anthropic_model` and an `or` tail in `api/runner.py`).

- [ ] **2. Keep a fresh checkout valid.** `_llm_provider_config` requires `llm_base_url` +
      `llm_model`; unconditional, that fires on every `Settings()` in the suite. Default them
      to the local mock (`http://127.0.0.1:8820/v1`) rather than dropping the validator — the
      dev default becomes "the mock gateway on this machine" instead of "the public Anthropic
      API", which is strictly safer than today and needs no credential.

- [ ] **3. Route the judge through the seam.** `evals/live_judge.py` is the *only* first-party
      importer of `anthropic`. The one thing it needs that the seam lacks is a truncation
      signal (`stop_reason == "max_tokens"`), which separates `ungraded` from a fabricated
      `unserved` — the module's own header records that conflating them mislabelled 65 of 190
      probes. Read `response_metadata` rather than the SDK, or fall back to the JSON-parse
      failure it already treats as `ungraded`.

- [ ] **4. Drop `anthropic` and `langchain-anthropic` from `pyproject.toml`.** Verified
      removable: `_sdk_exceptions` already returns `()` when the package is absent, measured,
      and every consumer guards on that. `llm_provider.py:219`'s `# pragma: no cover` becomes
      live code and wants a real test.

- [ ] **5. Make the config's own claim true.** "No provider client class is imported outside
      `agent/llm_provider.py`" is false today in three places and enforced by nothing — in
      fact `tests/test_third_party_layering.py` *licenses* three of them. Either make the
      sentence true and assert it, or rewrite it to what the tree actually guarantees.
      `cli/mock_llm.py` is legitimate (it is the server side) and `core/embeddings.py` is a
      parallel seam with no Anthropic arm at all; the rule has to name them or lose them.

- [ ] **6. Docs and lanes.** `.env.example` (test-pinned both ways), `README`, the runbook's
      caching prose, `infra/live/processes.sh`, `infra/live/e2e-full-stack/up.sh` (hard `die`
      on a missing `ANTHROPIC_API_KEY`), and `make chat`.

- [ ] **7. ADR**, recording: the exfiltration path this closes by construction; the caching
      cost accepted; why the provider concept goes rather than shrinking to one value; and the
      token-usage shapes that were Anthropic-specific (`turn_usage._cache_creation` reads
      `ephemeral_*` keys because LangChain zeroes the flat one on a write; `input` is a
      residual because the Anthropic adapter includes cached tokens where the API excludes
      them). `context_budget`'s ratio is clamped so a gateway that reports differently can
      only tighten the budget — expensive but safe.

## Scale

~24 test functions + 3 module constants out of 6,454. `tests/test_prompt_caching.py` (12
tests) is the only file whose whole subject disappears. The Helm chart already ships
`openai_compatible`, so `deploy/` needs no change.
