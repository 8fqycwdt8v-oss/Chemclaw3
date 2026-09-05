"""The LLM gateway seam (plan Phase F0) plus everything that rides its transport.

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings


class LlmSettings(BaseSettings):
    """The LLM gateway seam (plan Phase F0) plus everything that rides its transport.

    Grouped because these knobs configure the one OpenAI-compatible gateway and its uses: chat
    generation, per-task model routing (F10-E), the LLM-as-judge verifier (F10-B), the live-probe
    judge, and the embedding path (F10-A) — which reuses the LLM base_url/credential/TLS, so its
    provider knobs and the validator tying it to `llm_base_url` live here, in the section that
    owns that link.
    """

    # **Every model call goes to one OpenAI-compatible gateway, and which vendor sits behind it is
    # the gateway's business rather than this codebase's**
    # (`D-2026-09-04-a-gateway-is-the-only-provider`). There is no provider field: an `llm_provider`
    # naming a second SDK is what let `llm_base_url` be silently ignored on one of the two paths,
    # so a network-exposed pod configured with an internal gateway URL booted clean and sent every
    # prompt to the public vendor API. A destination that cannot be overridden by a mode selector
    # cannot be bypassed by one.
    #
    # The gateway is reached with **one generic API credential** (`llm_api_key`) — deliberately
    # *not* per-user Entra: the raw inference call is not a user-scoped resource (see
    # docs/archive/plans/foundation-plan.md §0). The TLS CA bundle, timeout and retry budget shape
    # the transport so an internal endpoint with a private CA works from config alone.
    # `llm_temperature`/`llm_max_tokens` are the default generation params threaded into the agent
    # (F0.3).
    #
    # **The defaults name the mock gateway on this machine**, so a fresh checkout is valid with no
    # credential and no endpoint — and the worst a misconfigured deployment can do is dial its own
    # loopback and be refused, loudly, on the first turn. The previous default was the public
    # Anthropic API, which failed *quietly* and in the exfiltrating direction. `make live-up` and
    # `infra/live/` start `chemclaw.cli.mock_llm` on exactly this address
    # (`cli/mock_llm.MOCK_PORT`); both are still validated as non-empty below, because an empty
    # base URL would hand the request back to the OpenAI SDK's own hardcoded public host.
    llm_base_url: str = "http://127.0.0.1:8820/v1"
    llm_model: str = "mock"
    # A `SecretStr`, like every other credential on this object
    # (`D-2026-08-26-a-credential-is-a-type-not-a-convention`): its `repr` is `**********`, so the
    # value cannot reach a log line, a `model_dump()` or a pydantic error message through a route
    # `core/logging.py`'s exact-match redaction has not been taught about. That filter stays and is
    # still the control; this is the type making the same guarantee where the filter is not looking.
    # Read it with `.get_secret_value()` — and note that an f-string does *not*, so a formatted
    # credential renders as asterisks and fails as a 401 rather than leaking.
    llm_api_key: SecretStr = SecretStr("")
    llm_tls_ca_bundle: str = ""
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=3, ge=0)

    # Ask an OpenAI-compatible endpoint to report token usage while streaming.
    #
    # **On by default because the alternative failed silently.** `ChatOpenAI` only default-enables
    # this when no custom base URL and no custom HTTP client are configured, and Chemclaw sets
    # both — so the endpoint was never asked, no usage chunk arrived, and every turn on the graph
    # engine metered zero while the budget guard went on admitting the next one. A setting rather
    # than a hardcoded `True` because upstream's caution is real: an endpoint that rejects
    # `stream_options` needs a way out that is not a code change.
    llm_stream_usage: bool = True

    # **A second endpoint to try when the first one is down** (AG-12). Empty — the default — means
    # no failover at all, so an existing deployment is unchanged and the whole mechanism is off
    # until somebody has a second endpoint to name.
    #
    # This is the one gap in the audit's agentic-engine list whose failure is total rather than
    # degraded: with a single endpoint, one outage fails *every* turn for the whole fleet once
    # `llm_max_retries` is spent, and neither the admission control nor the budget guard helps —
    # both assume the endpoint answers. Every other open row costs a worse answer; this one costs
    # the product.
    #
    # Only the base URL is required. The model and the credential fall back to the primary's,
    # because the common case is a second replica of the same internal deployment rather than a
    # different vendor — and a config that forced all three would make the cheap case verbose.
    llm_fallback_base_url: str = ""
    llm_fallback_model: str = ""
    # A `SecretStr` for the reason `llm_api_key` above states — and it was missing from
    # `core/logging.py`'s `_SECRET_SETTINGS` entirely until 2026-08-26, so the fallback
    # endpoint's key was the one credential in this file that no redaction covered at all.
    llm_fallback_api_key: SecretStr = SecretStr("")
    # Unset by default, and that default is load-bearing: current frontier models reject an
    # explicit `temperature` outright — `400 invalid_request_error: temperature is deprecated for
    # this model` — so a config that always sent one failed *every* turn on the then-default
    # provider. No test caught it because every test injects a fake chat client, so the parameter
    # never reached a real API.
    # `None` means "send no temperature and let the model use its own default"; a deployment on a
    # model that still accepts one sets it explicitly. Threaded into the agent by
    # `build_langgraph_agent`, which omits the key entirely when this is None (F0.3).
    llm_temperature: float | None = Field(default=None, ge=0)
    llm_max_tokens: int = Field(default=4096, gt=0)

    # How hard the model is asked to think before answering — the deployment's default, which a
    # profile may override per agent (`AgentProfile.effort`).
    #
    # **Unconditionally usable, and that is a widening this collapse bought.** It used to be
    # refused on the Anthropic path by two guards, because `ChatAnthropic` folded the same kwarg
    # into `output_config={'effort': ...}` **plus** an injected `thinking={'type': 'adaptive'}` —
    # extended thinking, a different feature with a `temperature` conflict and a claim on
    # `llm_max_tokens`. There was no intersection to publish; there were two parameters wearing one
    # name. With one client there is one meaning, so both guards are gone and `low | medium | high`
    # is simply what the gateway is asked for.
    #
    # **`None` means the key is absent from the request**, not present-and-null — the rule this
    # module records having broken every turn once, and it binds harder here than for
    # `temperature`: a 400 from a rejected parameter is deliberately *not* failed over
    # (`llm_provider._failover_exceptions`), so a parameter an endpoint dislikes fails every turn
    # rather than degrading to the fallback. Unset is therefore the shipped default, and a
    # deployment turns it on against an endpoint it has checked.
    #
    # `ChatOpenAI` is `extra="ignore"`, so a client that stopped accepting this kwarg — or a
    # gateway that does not understand it — would drop it in silence rather than raise, which is
    # why `tests/test_llm_effort.py` asserts the **request payload** rather than the attribute on
    # the constructed object. An earlier version of this comment cited `tests/test_llm_provider.py`
    # for an attribute assertion; that file contains no `effort`, and the assertion it described is
    # the one that missed all of the above.
    llm_effort: Literal["low", "medium", "high"] | None = None
    # **The model's context window, and until this existed no number anywhere in this tree was
    # one.** `agent_context_token_budget` is 100,000 by fiat, and neither it nor the static prefix
    # was ever compared to what the endpoint will actually accept: the whole handling of the ceiling
    # was retrospective, in `classify_model_failure`, after the request had been assembled, sent and
    # rejected.
    #
    # 0 means undeclared, which is the honest default for an endpoint whose window this repository
    # cannot know. Set it, and the conversation budget becomes the smaller of the configured budget
    # and `window - llm_max_tokens` — and this request's own measured prefix comes off whichever
    # wins (`agent/context_budget.py::effective_trigger`). **Declaring it is no longer what makes
    # the prefix count**, which is the correction worth reading here: that used to be true, no
    # deployment declared a window, and the ~43,000-token prefix was therefore charged against
    # nothing in every shipped configuration. It is charged unconditionally now, so this setting
    # does the one job its name says — bound the budget by what the endpoint can actually hold —
    # and is a *second* bound rather than the only real one.
    #
    # Per deployment rather than per `model_routes` entry, because the routes name *tasks* and the
    # window is a property of the endpoint every task shares. A deployment that routes tasks across
    # models with different windows should declare the smallest.
    llm_context_window_tokens: int = Field(default=0, ge=0)
    # Per-task model routing (plan F10-E). Maps a task name to the model id to use for it, so a
    # cheap model can run high-throughput/secondary steps (verification, classification) while
    # the frontier model drives the main reasoning turn — without a second provider or a second
    # import site (`build_chat_model(task)` stays the one place a model is built). Model ids are
    # whatever the gateway serves under that name; a task with no entry falls back to `llm_model`,
    # so an empty map (the default) is exactly today's single-model behavior. ENV override is JSON,
    # e.g. CHEMCLAW_MODEL_ROUTES='{"verifier": "internal-small", "agent": "internal-large"}'.
    model_routes: dict[str, str] = Field(default_factory=dict)
    # Answer verification & confidence routing (plan F10-B). When `verifier_enabled`, a drafted
    # answer is checked for citation faithfulness by an LLM-as-judge on the cheap routed model
    # (task `"verifier"`, F10-E): each factual claim is scored against the evidence it cites,
    # and an aggregate `confidence` in [0,1] is returned. An answer scoring below
    # `verifier_confidence_threshold` is flagged for human review (the confidence + the
    # unsupported claims ride on the turn's `AnswerEvent`), reusing the existing D-032 hold — no
    # new gate. When disabled (the default), the verifier falls back to the deterministic report
    # citation check (`report.harness.verify_claims`) so there is no network dependency and no
    # behavior change.
    verifier_enabled: bool = False
    verifier_confidence_threshold: float = Field(default=0.7, ge=0, le=1)
    # The judge call's own deadline. It is the one awaited call between the model's last token and
    # the AnswerEvent with no timeout beneath it, so a stalled judge endpoint was billed to
    # `service_turn_timeout_seconds` (600 s) — and a teardown landing in that stall is what rolled
    # back finished turns. Half `llm_timeout_seconds`' default and far under the turn deadline: a
    # verdict is one cheap structured call, and on expiry the verifier degrades to the offline
    # deterministic citation gate rather than holding the finished answer hostage.
    verifier_timeout_seconds: float = Field(default=30.0, gt=0)
    # Ceiling on the evidence rendered into one judge prompt, in characters (~a quarter of it in
    # tokens). The prompt used to embed every distinct tool output of the turn whole, so a 30-step
    # turn with ~20 kB results built a ~600 kB prompt — a judge call costing more than the turn it
    # graded, and past some length exceeding the judge model's own context, where the failure is
    # hard. The newest outputs are kept (they are what the answer was written from) and the
    # omitted ones are named to the judge so a claim resting on unrendered evidence is not marked
    # unsupported; the deterministic citation gate still checks every output regardless. Sized
    # like `gather_evidence_max_chars`, the same instrument one layer down.
    verifier_evidence_max_chars: int = Field(default=60_000, ge=1)
    # The review band around the threshold, inside which a verdict is re-rolled and decided by
    # the median (D-2026-08-27-a-verdict-at-the-margin-is-a-coin-toss). Measured, not chosen: the
    # judge's roll-to-roll spread is a margin effect — 0.000 over 32 rolls on grounded answers,
    # up to 0.167 deviation from the median exactly where the threshold lives — so the default is
    # that measured 0.167 rounded up to 0.2 (`make live-verifier-margin`, 2026-08-27, artifact in
    # docs/archive/). Re-fitting it on a deployment's own answers is the same command. `0`
    # switches the band off and restores the single-roll verdict. The cost is
    # `verifier_band_rerolls` extra judge calls only on answers that land inside the band, each
    # under its own `verifier_timeout_seconds`.
    verifier_review_band: float = Field(default=0.2, ge=0, le=0.5)
    verifier_band_rerolls: int = Field(default=2, ge=1)
    # The per-protocol condensation call's own deadline (`agent.condense`). Per *map unit*, so
    # one stalled extraction costs one row of the comparison and never the turn — the same
    # degrade-per-item rule the verifier applies to the whole answer, one level down. Larger than
    # the verifier's 30 s because the input is a whole procedure rather than a drafted answer, and
    # far under `service_turn_timeout_seconds` so a slow endpoint cannot hold a finished turn.
    #
    # The model itself is routed through `model_routes` under the task key `"protocol-digest"`, so
    # a deployment can point condensation at a cheap model without a second provider or a second
    # import site. No enable flag: it ships on, and the deterministic degrade below every failure
    # is what makes that honest with no credential present.
    protocol_digest_timeout_seconds: float = Field(default=45.0, gt=0)
    # The ungrounded-parameter scan over a drafted answer: shapes a chemist would read as
    # specification — a flow rate, a gradient table, a wavelength, a back pressure, a column brand,
    # an ICH limit, a polymorph form — marked for review when no tool in the turn produced them.
    #
    # Off by default and deliberately a deployment decision. It is a *shape* heuristic, not proof
    # of grounding: it both misses (an invented number in a shape it does not know) and over-fires
    # (a chemist's own figure quoted back). An answer marked for review that did not need it costs
    # trust in every mark after it, which is the failure mode that matters more here.
    #
    # The measured case for having it at all: a capability-boundary instruction cut invented
    # parameter classes from 9 to 1 across the six worst live probes, and a stronger model still
    # produced a complete branded HPLC method table *while writing* "not a validated method".
    # Prompting is necessary and demonstrably not sufficient.
    answer_shape_gate_enabled: bool = False
    # Embedding provider (plan F10-A). Selects how a note/query is embedded: `hash` is a
    # deterministic, offline, dependency-free feature-hash (dev/CI only — token-overlap
    # similarity, NOT neural-semantic); `openai_compatible` calls the internal endpoint's
    # `/embeddings` route (`embedding_model`), reusing the LLM base_url/credential/TLS
    # transport. `embedding_dim` must match both the model's output width and the
    # `note_index.embedding` column (`vector(N)` in infra/sql/012) — changing it is a new
    # migration, like the fingerprint bit width.
    embedding_provider: Literal["hash", "openai_compatible"] = "hash"
    embedding_model: str = ""
    embedding_dim: int = Field(default=1536, gt=0)
    # How many embedded texts to keep in memory (STO-12). Every retrieval embeds its query, and
    # the same query recurs constantly, so under `openai_compatible` each repeat was a network
    # round trip on the interactive path. Entries are keyed by provider+model+dim as well as the
    # text, so a config change can never serve the previous model's vectors. 0 disables the cache.
    embedding_cache_size: int = Field(default=2048, ge=0)
    # The most texts one provider request may carry. A reindex used to post the *entire* changed
    # set as a single request — a first run over a large corpus exceeded typical batch/token
    # ceilings, and because the failure was all-or-nothing under retry, the retry re-sent the
    # same oversized payload. Chunking bounds the request; order is preserved across chunks.
    embedding_batch_size: int = Field(default=256, ge=1)

    @model_validator(mode="after")
    def _gateway_is_addressed(self) -> Self:
        """The gateway needs an address and a model name, or the client cannot be built.

        **Unconditional, where this used to be scoped to one value of a provider field.** That
        scoping is what let the other value ignore `llm_base_url` entirely: a deployment could set
        a gateway URL, pass every validator, and have the client resolve to the public vendor host
        anyway. With one client there is one destination, so the check that guards it applies to
        every configuration there is.

        Both fields default to the local mock gateway, so this fires only on a deployment that
        explicitly blanks one — which is the case that matters, because an empty `base_url` is not
        "no destination", it is the OpenAI SDK's own hardcoded public host. Checked at startup so a
        half-configured endpoint fails here with a clear message rather than as an opaque
        connection/404 error on the first model call.
        """
        required = (("llm_base_url", self.llm_base_url), ("llm_model", self.llm_model))
        missing = [name for name, value in required if not value]
        if missing:
            raise ValueError(
                f"the LLM gateway requires {', '.join(missing)} to be set — an empty base URL is "
                "not 'no destination', it is the provider SDK's own public host"
            )
        return self

    @model_validator(mode="after")
    def _embedding_provider_config(self) -> Self:
        """`openai_compatible` embeddings need a model name — the endpoint is already required.

        The embedding path reuses the LLM transport, so a half-configured pair has to be rejected
        at startup instead of surfacing as an opaque connection error on the first note-index or
        query embedding deep in the retrieval path. `embedding_model` is the whole check:
        it has no default, because no gateway serves embeddings under a name this repository
        can guess.

        **`llm_base_url` is deliberately not re-checked here, and saying so is the point.** It was,
        with a docstring claiming the re-check caught a deployment that blanked it — and the branch
        could never run: `_gateway_is_addressed` above is unconditional and declared first, so
        pydantic raises on an empty base URL before this validator is reached, whatever the
        embedding provider is. The test that covered it passed on the *other* validator's message.
        Nothing is weakened by the removal; the surviving check is strictly the wider one.
        """
        if self.embedding_provider == "openai_compatible" and not self.embedding_model:
            raise ValueError(
                "embedding_provider='openai_compatible' requires embedding_model to be set"
            )
        return self
