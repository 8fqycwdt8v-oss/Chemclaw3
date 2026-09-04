# D-2026-09-04-a-gateway-is-the-only-provider — the provider concept is deleted, not narrowed

**Status:** accepted · **Date:** 2026-09-04

## Context

`llm_provider` was a two-value seam — `openai_compatible` for the internal endpoint,
`anthropic` as a local-dev path — and `anthropic` was the shipped default. The intent was always
one gateway: `core/config/llm.py` said so, and `deploy/helm/chemclaw/values.yaml` has shipped
`openai_compatible` for as long as the chart has existed.

The dev path was not a smaller version of the production one. It was a second client, reached by a
branch, reading a different subset of the same settings — and three controls disagreed about what
that meant.

### The three controls agreed a pod exfiltrating every prompt was correctly configured

`_anthropic_model` passed no base URL of any kind, so `llm_base_url` was **ignored** on that path.
`api/middleware.py::_refuse_public_llm_exposure` returned early whenever `llm_base_url` was truthy,
and its docstring asserted in the present tense that an `llm_base_url` "naming an
anthropic-compatible gateway" satisfied it. `core/netguard.py` added `api.anthropic.com` to the
egress allowlist whenever the provider was `anthropic`. All three read the same field and reached
the same wrong conclusion.

Measured on the shipped defaults plus one line an operator would obviously write:

```
                            BEFORE                AFTER
client                      ChatAnthropic         ChatOpenAI
destination it will dial    api.anthropic.com     gateway.internal/v1
llm_base_url configured     gateway.internal/v1   gateway.internal/v1
the boot guard              PASSED (boots)        replaced (see below)
api.anthropic.com allowed   True                  False
```

And with nothing configured at all, `netguard.derive_allowed` went from
`['127.0.0.1', 'api.anthropic.com', 'localhost']` to `['127.0.0.1', 'localhost']`.

So a network-exposed pod configured with an internal gateway booted clean, passed the guard that
exists to prevent exactly this, and sent every prompt and completion to a public vendor. Nothing
tested that combination, and the sentence that made it plausible was a present-tense claim about a
control — the highest-yield shape this repository already warns about.

Two further gaps sat on the same path, which was the *default*: `llm_fallback_base_url` was never
applied (`build_chat_model` returned the anthropic client without `_with_failover`, on the provider
whose own config comment calls failover "the one gap whose failure is total rather than degraded"),
and `evals/live_judge.py` posted the **Anthropic protocol** to a doubled `/v1/v1/messages` path
against a gateway, degrading every probe to `ungraded` rather than erroring.

## Decision

**There is no provider.** `llm_provider` is deleted — not reduced to a one-value `Literal`. A
gateway is `llm_base_url` + `llm_model` + `llm_api_key`, and which vendor sits behind it is the
gateway's business.

A one-value enum was rejected: it leaves all nine readers in place and the next vendor one commit
away, which is scaffolding for a decision already taken.
`tests/test_config.py::test_no_provider_field_survives` is what holds it.

The exfiltration path is **unrepresentable** rather than fixed. There is one branch, so
`llm_base_url` either reaches the client or
`tests/test_llm_provider.py::test_a_configured_gateway_is_where_the_model_is_built` is red.

### The default is a loopback mock

`llm_base_url` defaults to `http://127.0.0.1:8820/v1` and `llm_model` to `mock` — the address
`cli/mock_llm.py` already serves in exactly this shape, and the name `infra/live/soak.sh` and the
runbook already use. A fresh checkout stays valid and needs no credential.

Argued both ways, because a defaulted address is a real hazard. The failure mode is a pod that
inherits the default and dials **itself**: it cannot leave the pod and it fails loudly on the first
turn. The old default failed *quietly*, in the exfiltrating direction. Strictly safer — but "loudly
on the first turn" is still worse than "at boot", so `_refuse_public_llm_exposure` is replaced by
`_refuse_unconfigured_llm_gateway`: a non-loopback bind with a loopback `llm_base_url` refuses to
start. Note what changed in kind — the old guard was a *destination policy*, the new one is a
*configuration* check. Where a real gateway points is the operator's call.

The validator is now unconditional, so it fires only on a deployment that explicitly blanks a
field. Dropping it was rejected: an empty `base_url` means *the OpenAI SDK's own public host*,
which is the same hole one vendor over.

## Consequences

**Prompt caching is gone, and the loss is smaller than it looks.** `prompt_caching_middleware`,
`_CachingDisabled` and `llm_prompt_caching` are deleted. `cache_control: {"type": "ephemeral"}` has
no counterpart on an OpenAI-compatible endpoint — `langchain_openai` contains zero occurrences of
it — so the mechanism was **never reachable from the production path**. What actually changed is
that the dev path lost a saving production never had. `chemclaw_cache_write_tokens_total` should now
be expected to read a flat 0.

**Token usage.** `_cache_creation` is deleted as dead: the `ephemeral_5m_input_tokens` /
`ephemeral_1h_input_tokens` keys came only from `langchain_anthropic`'s reader, which nothing
installed now produces. Its knowledge is preserved as an assertion rather than a comment —
`tests/test_upstream_surface.py` pins both flat key names **and the absence of `ephemeral_*`**, so
if upstream ever adds a per-TTL split the helper comes back instead of the tokens quietly moving
into `input`. `input` stays a *residual* for a newly measured reason:
`langchain_openai._create_usage_metadata` sets `input_tokens = prompt_tokens`, which OpenAI defines
as including the cached share. `context_budget`'s `estimator_ratio` is unchanged and still clamped
so it can only tighten.

**Effort is a widening.** `AgentProfile.effort` was refused on the anthropic path by two guards,
because `ChatAnthropic` folded `reasoning_effort` into `output_config.effort` plus an injected
`thinking={'type':'adaptive'}`. Both guards are gone and `effort` is now unconditional.
`tests/test_llm_effort.py` keeps its payload-not-attribute shape, because both clients are
`extra="ignore"` and a gateway that does not understand `reasoning_effort` drops it in silence.

**The judge goes through the seam.** `live_probe_judge_model` — a vendor model id checked into this
repository — becomes `model_routes["live-probe-judge"]`, which is what that mechanism exists for.
The truncation signal is preserved and *widened*: `_truncated()` reads `finish_reason` **or**
`stop_reason`, because LangChain does not normalise it and a gateway relaying a vendor's own field
may say either. That distinction is load-bearing — the module's own header records that conflating
truncation with a verdict mislabelled **65 of 190** probes. An unset route falls back to the model
under test, which would silently make the run self-grading, so the client is cached once per run and
logs a WARNING naming the fallback.

**`chemclaw_model_calls_total`'s `provider` label is removed.** One possible value is cardinality
that answers nothing. Followed through into `core/metrics.py`, the Grafana dashboard, the
PrometheusRule and the runbook.

**Two second-order removals, both controls in name only, both found by this change.**
`condense_protocols` wrapped client construction in a `try/except` whose comment claimed "no
reachable route is the deployment state this degrade exists for". It only ever fired because
`_anthropic_model` preflighted `ANTHROPIC_API_KEY`; with one gateway, construction is pure config
and never raises. It was also *wrong*: it returned every row `digest_source="recorded"` with
`complete=True`, claiming every protocol had been read when none had. Reachability is discovered per
protocol, which `_read_prose` was already written to do. Likewise D-037's credential preflight in
`cli/chat.py` — an empty `CHEMCLAW_LLM_API_KEY` is legitimate, because many internal gateways ignore
the bearer. Both docstrings asserted their control in the present tense.

**One absence guard was deleted with `tests/test_prompt_caching.py` and deliberately not rebuilt.**
`test_no_module_level_call_dials_the_provider_at_collection`, from
`D-2026-08-26-a-guard-that-runs-at-collection-guards-nothing`, has nothing left to guard: no module
performs a credential probe at import. A general "no call in `skipif`" rule would need an allowlist
of its own exceptions (`test_deploy_chart.py` legitimately calls `shutil.which("helm")`). That rule
still stands in its own ADR; the honest enforcement is collecting under the egress guard, which is a
new mechanism rather than a restored test.

**The live lane's shape changes.** `infra/live/e2e-full-stack/up.sh` no longer dies without
`ANTHROPIC_API_KEY`. Without a gateway it runs against `cli/mock_llm`, so that lane exercises every
hop against scripted completions unless a real gateway is named. **A bare vendor key is no longer
usable by `src/`** — it has to reach a gateway, which is the point.

**The seam's own rule is now true and asserted.** `core/config/llm.py` claimed "No provider client
class is imported outside `agent/llm_provider.py`", which was false in three places and enforced by
nothing — `tests/test_third_party_layering.py` in fact *licensed* three packages to import the llm
stack. The partition is now stated as what the tree guarantees and AST-scanned:
`agent/llm_provider.py` and `core/embeddings.py` may name a client (the second cannot go through
`build_chat_model`, which builds a *chat* model), `cli/mock_llm.py` may name response **types** only,
and no first-party module imports the `anthropic` SDK at all. The duplication behind the second
exception is gone: `core/http.private_ca_transport` states the CA-context + `trust_env=False`
decision once, and both seams take it.

**One control was lost and not rebuilt.** The judge passed `trust_env=False` on its own client;
through the seam it gets that only when `llm_tls_ca_bundle` is set — a hole the *agent* already had.
Levelling it up is one decision for the whole seam with real blast radius, since `trust_env=False`
also stops httpx reading `SSL_CERT_FILE`. Filed as a `BACKLOG.md` §1 row with its anchor.

`anthropic` and `langchain-anthropic` leave `pyproject.toml`. `_sdk_exceptions` becomes
`_openai_exceptions` — no module-name argument, no `try: import`, and therefore no unreachable
branch, which removes a `# pragma: no cover` by deletion rather than by suppression.
