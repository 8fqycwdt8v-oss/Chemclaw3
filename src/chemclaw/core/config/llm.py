"""Settings for the LLM gateway and everything that rides its transport.

One domain section of the composed `Settings`; the package `__init__.py` flattens the sections and
owns the env prefix, `.env` loading and cross-section validators.
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

    # Every model call goes to one OpenAI-compatible gateway; which vendor sits behind it is the
    # gateway's business. There is no provider field, so nothing can bypass this destination. One
    # generic API credential (`llm_api_key`), not per-user Entra: inference is not a user-scoped
    # resource. The defaults name the local mock gateway (`cli/mock_llm.MOCK_PORT`), so a
    # misconfigured deployment fails loudly on loopback; an empty base URL is refused because the
    # OpenAI SDK would fall back to its public host.
    llm_base_url: str = "http://127.0.0.1:8820/v1"
    llm_model: str = "mock"
    # Opt-in to a gateway on this host (dev mock or same-pod sidecar). Every model-calling process
    # refuses to boot on a loopback `llm_base_url` without it
    # (`core/llm_gateway.refuse_unconfigured_llm_gateway`), since the shipped default is loopback.
    llm_allow_loopback_gateway: bool = False
    # A `SecretStr`, so the value cannot reach logs, dumps or validation errors; read it with
    # `.get_secret_value()` (an f-string renders asterisks).
    llm_api_key: SecretStr = SecretStr("")
    llm_tls_ca_bundle: str = ""
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=3, ge=0)

    # Ask the endpoint to report token usage while streaming. `ChatOpenAI` skips this with a custom
    # base URL or HTTP client, which would meter every turn at zero. A setting so an endpoint that
    # rejects `stream_options` can turn it off.
    llm_stream_usage: bool = True

    # A second endpoint to try when the first is down; empty disables failover. Without it one
    # outage fails every turn once `llm_max_retries` is spent. Model and credential default to the
    # primary's, since the common case is a second replica.
    llm_fallback_base_url: str = ""
    llm_fallback_model: str = ""
    # A `SecretStr` for the same reason as `llm_api_key`.
    llm_fallback_api_key: SecretStr = SecretStr("")
    # `None` sends no temperature: current frontier models reject an explicit one with a 400. Set it
    # for a model that accepts it; `build_langgraph_agent` omits the key when None.
    llm_temperature: float | None = Field(default=None, ge=0)
    llm_max_tokens: int = Field(default=4096, gt=0)

    # The deployment's default reasoning effort; a profile may override it (`AgentProfile.effort`).
    # `None` omits the key, and stays the default because a 400 for a rejected parameter is not
    # failed over (`llm_provider._failover_exceptions`). `ChatOpenAI` ignores unknown kwargs
    # silently, so `tests/test_llm_effort.py` asserts the request payload.
    llm_effort: Literal["low", "medium", "high"] | None = None
    # The model's context window in tokens; 0 means undeclared. When set, the conversation budget is
    # the smaller of `agent_context_token_budget` and `window - llm_max_tokens`, minus the measured
    # prefix (`agent/context_budget.py::effective_trigger`). Per deployment: with mixed-window
    # routes, declare the smallest.
    llm_context_window_tokens: int = Field(default=0, ge=0)
    # BPE encoding `agent/context_budget.py` counts the request prefix with, instead of chars/4. A
    # name, not a model id, because the gateway does not say what it fronts. Must resolve from the
    # cache baked into the image (`TIKTOKEN_CACHE_DIR`); without it the budget logs once at INFO and
    # falls back to chars/4, never touching the network. Empty keeps the estimator deliberately.
    llm_token_encoding: str = "o200k_base"
    # Per-task model routing: task name → model id, so cheap models run secondary steps;
    # `build_chat_model(task)` stays the one place a model is built. Unrouted tasks use `llm_model`.
    # JSON in the env, e.g. CHEMCLAW_MODEL_ROUTES='{"verifier": "internal-small"}'.
    model_routes: dict[str, str] = Field(default_factory=dict)
    # Answer verification: an LLM judge (task `"verifier"`) scores each claim against the evidence
    # it cites into a `confidence` in [0,1]; below `verifier_confidence_threshold` the answer is
    # flagged for review on its `AnswerEvent`. When disabled, the deterministic citation check
    # (`report.harness.verify_claims`) runs instead.
    verifier_enabled: bool = False
    verifier_confidence_threshold: float = Field(default=0.7, ge=0, le=1)
    # The judge call's deadline, far under the turn deadline; on expiry the verifier degrades to the
    # deterministic citation gate rather than holding the finished answer.
    verifier_timeout_seconds: float = Field(default=30.0, gt=0)
    # Ceiling on evidence characters rendered into one judge prompt. The newest outputs are kept and
    # omitted ones are named to the judge; the deterministic citation gate still checks every
    # output.
    verifier_evidence_max_chars: int = Field(default=60_000, ge=1)
    # Band around the threshold inside which a verdict is re-rolled and decided by the median, sized
    # to the judge's measured roll-to-roll spread at the margin (`make live-verifier-margin` refits
    # it). 0 restores single-roll verdicts. Costs `verifier_band_rerolls` calls only inside the
    # band.
    verifier_review_band: float = Field(default=0.2, ge=0, le=0.5)
    # How many times a flagged answer is sent back to be answered again within the same turn. Counts
    # only agent-initiated rounds (a per-turn local in `api/runner.py`), so a chemist's follow-up
    # gets a fresh allowance; each round is still counted by `loop_cap`/`spend_cap`. Reachable only
    # behind `verifier_enabled` or `answer_shape_gate_enabled`. 0 disables.
    # `core/config/__init__.py` bounds it against the turn deadline.
    answer_review_max_rounds: int = Field(default=2, ge=0)
    # Whether an answer still flagged after the review rounds opens a durable `review` wait
    # (`durable/awaiting.py`) so a person looks at it. Fires only when rounds are enabled and bought
    # nothing. The answer still ships; a wait that cannot be opened is logged and skipped
    # (`api/runner.py::_escalate_exhausted_review`).
    answer_review_escalation_enabled: bool = True
    verifier_band_rerolls: int = Field(default=2, ge=1)
    # Deadline for one protocol condensation call (`agent.condense`), per map unit so a stall costs
    # one comparison row, not the turn. Routed via `model_routes["protocol-digest"]`; always on,
    # with a deterministic degrade.
    protocol_digest_timeout_seconds: float = Field(default=45.0, gt=0)
    # Scan a drafted answer for ungrounded specification shapes (flow rate, gradient table,
    # wavelength, column brand, ICH limit, polymorph form…) that no tool in the turn produced, and
    # mark it for review. A shape heuristic that both misses and over-fires (pinned in
    # `tests/test_verifier.py`); on by default because it is deterministic and costs no model call,
    # and prompting alone does not stop invented parameters.
    answer_shape_gate_enabled: bool = True
    # `hash` is an offline feature-hash for dev/CI (token overlap, not semantic);
    # `openai_compatible` calls the gateway's `/embeddings` with `embedding_model`. `embedding_dim`
    # must match the model and the `note_index.embedding` column (`vector(N)`); changing it is a new
    # migration.
    embedding_provider: Literal["hash", "openai_compatible"] = "hash"
    embedding_model: str = ""
    embedding_dim: int = Field(default=1536, gt=0)
    # In-memory embedding cache entries, keyed by provider+model+dim and text, so a config change
    # never serves stale vectors. 0 disables.
    embedding_cache_size: int = Field(default=2048, ge=0)
    # Most texts per embedding request; a reindex is chunked, preserving order.
    embedding_batch_size: int = Field(default=256, ge=1)

    @model_validator(mode="after")
    def _gateway_is_addressed(self) -> Self:
        """The gateway needs an address and a model name, or the client cannot be built.

        Unconditional, so there is one destination for every configuration. Fires only when a field
        is explicitly blanked; an empty `base_url` would be the OpenAI SDK's public host.
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

        `embedding_model` has no default because no gateway serves embeddings under a guessable
        name. `llm_base_url` is already enforced by `_gateway_is_addressed`, which runs first.
        """
        if self.embedding_provider == "openai_compatible" and not self.embedding_model:
            raise ValueError(
                "embedding_provider='openai_compatible' requires embedding_model to be set"
            )
        return self
