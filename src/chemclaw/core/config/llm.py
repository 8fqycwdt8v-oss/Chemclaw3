"""The LLM provider seam (plan Phase F0) plus everything that rides its transport.

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class LlmSettings(BaseSettings):
    """The LLM provider seam (plan Phase F0) plus everything that rides its transport.

    Grouped because these knobs configure the one internal (or dev-Anthropic) endpoint and its
    uses: chat generation, per-task model routing (F10-E), the LLM-as-judge verifier (F10-B),
    and the embedding path (F10-A) — which reuses the LLM base_url/credential/TLS, so its
    provider knobs and the validator tying it to `llm_base_url` live here, in the section that
    owns that link.
    """

    # The agent's chat client is selected by config, so the deployment can point the agent at
    # the internal OpenAI-compatible ("OpenLLM-like") endpoint without any code change, keeping
    # Anthropic as a local-dev path. `openai_compatible` reaches the endpoint with **one generic
    # API credential** (`llm_api_key`) — deliberately *not* per-user Entra: the raw inference
    # call is not a user-scoped resource (see docs/archive/plans/foundation-plan.md §0).
    # `llm_base_url`/`llm_model` are required for `openai_compatible` (validated below); the TLS
    # CA bundle, timeout, and retry budget shape the transport so an internal endpoint with a
    # private CA works from config alone. `llm_temperature`/`llm_max_tokens` are the default
    # generation params threaded into the agent (F0.3). The default provider is `anthropic` so a
    # fresh checkout config singleton is valid with no endpoint set; production sets
    # `CHEMCLAW_LLM_PROVIDER=openai_compatible` + the base_url/model. No provider client class
    # is imported outside `agent/llm_provider.py`.
    llm_provider: Literal["openai_compatible", "anthropic"] = "anthropic"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
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
    # Unset by default, and that default is load-bearing: current frontier models (the shipped
    # `agent_model`, claude-sonnet-5) reject an explicit `temperature` outright —
    # `400 invalid_request_error: temperature is deprecated for this model` — so a config that
    # always sent one failed *every* turn on the default Anthropic path. No test caught it
    # because every test injects a fake chat client, so the parameter never reached a real API.
    # `None` means "send no temperature and let the model use its own default"; a deployment on a
    # model that still accepts one sets it explicitly. Threaded into the agent by `build_agent`,
    # which omits the key entirely when this is None (F0.3).
    llm_temperature: float | None = Field(default=None, ge=0)
    llm_max_tokens: int = Field(default=4096, gt=0)
    # Anthropic prompt caching: mark the static prefix — the tool schemas and the system prompt —
    # so a repeat call reads it at roughly a tenth of the input price instead of re-sending it at
    # full price. Read only by `agent/llm_provider.prompt_caching_middleware`, which is also the
    # only place that knows caching is provider-specific.
    #
    # **On by default, because the prefix here is large, static, and re-sent on every model call.**
    # Measured on the default profile: 25,548 characters of system prompt across two blocks plus 29
    # tool schemas — 21,321 tokens that are byte-identical for the whole life of a profile,
    # ahead of a conversation tail that is not. Measured live before this existed, across 22 billed
    # turns: `cache_read_tokens = 0` and `cache_write_tokens = 0` on every one of them, an
    # input:output ratio of 199:1, and single turns reaching 260,000 input tokens. A cache write
    # costs 1.25x, a read 0.1x, so two calls over one prefix already pay for the write — and a
    # single agent turn makes one model call per tool round trip, so break-even arrives inside the
    # first turn rather than across turns.
    #
    # Off is for a deployment that has measured the opposite: turns rarer than the 5-minute TTL and
    # exactly one model call each, where every write is paid and never read.
    llm_prompt_caching: bool = True
    # Per-task model routing (plan F10-E). Maps a task name to the model id to use for it, so a
    # cheap model can run high-throughput/secondary steps (verification, classification) while
    # the frontier model drives the main reasoning turn — without a second provider or a second
    # import site (`build_chat_model(task)` stays the one place a model is built). Model ids
    # are for the *active* provider (an `openai_compatible` model name, or an Anthropic one); a
    # task with no entry falls back to the provider's default (`llm_model`/`agent_model`), so an
    # empty map (the default) is exactly today's single-model behavior. ENV override is JSON,
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
    # The automatic challenge round (`agent/challenge_gate.py`). When enabled, a finished answer is
    # attacked by a panel of independent agents before it is delivered — and the panel's angles are
    # *generated for the task*, not declared here, because a fixed persona list is a guess about
    # what a given answer gets wrong.
    #
    # **The trigger is the shape of the turn, not only its confidence.** A turn that spawned two or
    # more subagents is a *team*, and a team is challenged unconditionally: work split across agents
    # is exactly where no single context saw the whole thing, so the confidence signal that reads
    # one answer is the wrong instrument. A turn that delegated once or not at all is challenged
    # only when `verifier_enabled`/`answer_shape_gate_enabled` already flagged it — those checks
    # keep their existing meaning and this adds a second opinion where they fire.
    #
    # Off by default for the reason `agent_teams_enabled` is (`agent/team.py`): a panel that
    # over-flags is worse than no panel, and which of those this deployment gets is a measurement
    # nobody has taken yet. Turning it on with both checks above off gives a team-only challenge,
    # which is a coherent deployment rather than a misconfiguration.
    challenge_enabled: bool = False
    # One challenger's deadline. Same argument as `verifier_timeout_seconds`: the panel sits between
    # the model's last token and the AnswerEvent, so an unreachable endpoint must cost the challenge
    # and never the answer. On expiry a challenger returns "no objection" — the panel degrades to
    # the opinions that did arrive rather than holding a finished answer hostage.
    challenge_timeout_seconds: float = Field(default=30.0, gt=0)
    # How many angles the panel is asked for. Bounded at both ends: one challenger is an opinion
    # rather than a panel, and past a handful the marginal angle is a rephrasing of an earlier one
    # while every member costs a model call on the answer's hot path.
    challenge_panel_size: int = Field(default=3, ge=1, le=6)
    # How many challengers must corroborate before the objection is acted on. A quorum rather than
    # any-one-objects because a single adversarial persona instructed to find fault will find fault;
    # requiring agreement between independently-briefed angles is what separates a real defect from
    # one challenger's enthusiasm. Clamped to the panel size at read time, so a quorum larger than
    # the panel cannot silently mean "never".
    challenge_quorum: int = Field(default=2, ge=1)
    # How many revision rounds a corroborated objection may force. Zero means "never revise, surface
    # it" — a coherent choice for a deployment that wants the chemist, not the model, to resolve
    # every objection. The bound exists because the panel and the model can disagree indefinitely,
    # and an unbounded argument between them is a turn that never ends.
    challenge_max_attempts: int = Field(default=1, ge=0)
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

    @model_validator(mode="after")
    def _llm_provider_config(self) -> Self:
        """`openai_compatible` needs an endpoint and a model, or the client cannot be built.

        Checked at startup so a half-configured provider fails here with a clear message rather
        than as an opaque connection/404 error on the first model call. The `anthropic` dev path
        needs neither (it reads its key/model elsewhere), so the check is provider-scoped.
        """
        if self.llm_provider == "openai_compatible":
            required = (("llm_base_url", self.llm_base_url), ("llm_model", self.llm_model))
            missing = [name for name, value in required if not value]
            if missing:
                raise ValueError(
                    f"llm_provider='openai_compatible' requires {', '.join(missing)} to be set"
                )
        return self

    @model_validator(mode="after")
    def _embedding_provider_config(self) -> Self:
        """`openai_compatible` embeddings need the shared endpoint and a model name.

        The embedding path reuses the LLM transport (`llm_base_url`), which stays empty under
        the default `anthropic` chat provider — so the combination must be rejected at startup
        instead of surfacing as an opaque connection error on the first note-index or query
        embedding deep in the retrieval path.
        """
        if self.embedding_provider == "openai_compatible":
            required = (
                ("llm_base_url", self.llm_base_url),
                ("embedding_model", self.embedding_model),
            )
            missing = [name for name, value in required if not value]
            if missing:
                raise ValueError(
                    f"embedding_provider='openai_compatible' requires "
                    f"{', '.join(missing)} to be set"
                )
        return self
