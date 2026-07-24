"""Single, typed source of every environment-dependent value in Chemclaw.

Why this exists: the plan forbids magic numbers and second config sources
(CLAUDE.md "Config, never magic numbers"; plan step 0.3). Every URL, DSN, queue
name, and timeout that code or infrastructure needs is declared here once, is
type-checked, and is overridable via environment variables or a local `.env`
file. `infra/docker-compose.yml` is wired to the same variable names, so the app
and the dev stack can never drift apart.

Usage:
    from chemclaw.config import settings
    client_target = settings.temporal_address

Only fields that are actually consumed (by code or by the compose stack) live
here — no speculative "for later" settings. New phases add their own fields when
the first real consumer lands.

Structure: the flat `Settings` class is composed from one mixin per domain
(`class Settings(ObservabilitySettings, TemporalSettings, ...)`). Each mixin
holds its section's fields, validators, and derived properties, so a reader
finds everything about one concern in one place — while the composed class
keeps every attribute flat (`settings.postgres_dsn`) and every env name
unprefixed-by-section (`CHEMCLAW_POSTGRES_DSN`), exactly as before the split.
A cross-field validator lives in the section that owns the relationship.
"""

import os
import sys
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class McpServerSpec(BaseModel):
    """Launch spec for one stdio MCP server the agent attaches as a capability (D-029).

    `command` + `args` are how MAF's `MCPStdioTool` spawns the server as a subprocess;
    `allowed_tools` restricts which of that server's tools the conversational agent may call
    (the write/index tools are excluded — ingestion writes go through the PR-gate, not chat).
    """

    name: str
    command: str
    args: list[str]
    allowed_tools: list[str] | None = None


class ObservabilitySettings(BaseSettings):
    """Logging, the GxP tool-audit trail, and OpenTelemetry export.

    Grouped because these are the process-wide "what happened" knobs: one
    config-driven switch for verbosity so an admin can raise it to DEBUG for
    troubleshooting without touching code, the audit-record shape, and the
    (off-by-default) OTel pipeline. Applied once per process by
    `chemclaw.logging.configure_logging`, called at each worker's entrypoint.
    """

    # The format carries the timestamp, level, and logger name every diagnosis needs.
    log_level: str = "INFO"
    log_format: str = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    # GxP tool-audit trail (agents.audit): every agent tool call is logged once (name, args,
    # outcome, latency) by one MAF function middleware. Arguments are truncated to this many
    # characters so a large payload (a full optimization problem, an observation list) cannot
    # flood the log; raise it when a fuller argument record is needed for an audit.
    agent_audit_max_arg_chars: int = Field(default=200, ge=0)
    # The deployment's code/prompt/skill revision stamped onto every audit record (AG-14): the
    # Git SHA or image digest the running pod was built from, so a past agent result ties to the
    # exact version that produced it (GxP reproducibility). The deployment sets it (the F6 image
    # build injects the digest); "unknown" until then, a value change, not a schema change.
    deployment_revision: str = "unknown"
    # OpenTelemetry export (off by default). When enabled, `chemclaw.logging.configure_telemetry`
    # calls MAF's `configure_otel_providers`, which reads the standard `OTEL_EXPORTER_OTLP_*`
    # environment variables for the collector endpoint. Requires the OpenTelemetry SDK + OTLP
    # exporter extras to be installed; `enable_sensitive_data` controls whether prompts/results
    # are attached to spans (keep off unless a trusted collector needs them).
    otel_enabled: bool = False
    otel_include_sensitive_data: bool = False
    # The OTLP collector endpoint (plan F6-T5). Exported as `OTEL_EXPORTER_OTLP_ENDPOINT` for MAF's
    # `configure_otel_providers` when set; empty in dev (no collector). Config, so the in-cluster
    # collector address is one value like every other endpoint.
    otel_endpoint: str = ""


class TemporalSettings(BaseSettings):
    """Temporal — durable execution of long scientific jobs (plan Phase 1).

    Grouped because everything here shapes how the app reaches and uses the one
    Temporal cluster: the frontend endpoint, transport security, the two task
    queues from the architecture, and the shared activity retry bound.
    """

    # `address` is the frontend gRPC endpoint; `namespace` isolates a team's jobs.
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    # Securing the Temporal transport (plan F4-T6, §7.2): one of the two non-Entra bridges. Identity
    # rides *inside* the workflow payload (`requested_by`, F4-T3), never the transport — so the
    # transport is authenticated with mTLS (client cert/key + server-root CA, paths to PEM files) or
    # a Temporal Cloud API key, not with a user token. All empty in local dev (a plaintext dev
    # broker); a deployment sets the mTLS trio or the API key.
    temporal_tls_cert: str = ""
    temporal_tls_key: str = ""
    temporal_tls_ca: str = ""
    temporal_api_key: str = ""

    # The two task queues from the architecture: heavy HPC jobs vs. light
    # background jobs (sync/re-index/reports). Names are config so a deployment
    # can shard or rename queues without touching worker code (D-006).
    hpc_task_queue: str = "hpc-jobs"
    background_task_queue: str = "background-jobs"

    # Bound on retries for ordinary activities under the shared bad-data retry policy
    # (`workflows.publish.BAD_DATA_RETRY`). Bad data is non-retryable by type; this caps
    # the *transient* retries so an unclassified deterministic failure (a bug, not a
    # network blip) gives up instead of pinning a worker with unlimited retries.
    activity_max_attempts: int = Field(default=5, ge=1)

    @model_validator(mode="after")
    def _temporal_mtls_is_complete(self) -> Self:
        """A Temporal client cert without its key (or vice versa) is a silent half-config.

        mTLS needs both the client cert and its private key; a server-root CA alone (server-auth
        only) is fine. Rejecting cert-xor-key at startup beats a confusing handshake failure later.
        """
        if bool(self.temporal_tls_cert) != bool(self.temporal_tls_key):
            raise ValueError("temporal_tls_cert and temporal_tls_key must be set together")
        return self


class StoreSettings(BaseSettings):
    """Postgres/pgvector — fingerprint store (Phase 3) and QM result cache (plan step 1.10).

    Grouped because these are the database-transport knobs every store connection
    shares: one DSN for the whole app plus the connect/statement timeouts.
    """

    postgres_dsn: str = "postgresql://chemclaw:chemclaw@localhost:5432/chemclaw"
    # Fail fast when the database is unreachable instead of hanging until the
    # enclosing activity's start-to-close timeout expires (libpq connect_timeout).
    pg_connect_timeout_seconds: int = Field(default=10, gt=0)
    # Per-statement wall-clock bound for the store connections (libpq
    # statement_timeout). A hung query is cancelled after this instead of consuming
    # the whole enclosing activity's start-to-close budget. 0 disables it; migrations
    # deliberately connect without a statement timeout (an index build may be slow).
    pg_statement_timeout_seconds: float = Field(default=30.0, ge=0)


class HpcSettings(BaseSettings):
    """QM job timeouts, the mock-HPC spine, and the real Nextflow launcher (plan 1.2–1.4, F5).

    Grouped because the qm_* and hpc_* knobs describe one execution path — the QM
    activities and the HPC backend they dispatch to — and the poll/heartbeat
    relationship between them is validated here, in the section that owns it.
    """

    # QM job timeouts and mock-HPC timing (plan steps 1.2–1.4). Times are in
    # seconds. The "mock_*" values only shape the simulated HPC job's duration
    # so the durable path is observable; they vanish when a real backend lands.
    qm_activity_timeout_seconds: float = Field(default=30.0, gt=0)
    # Heartbeat timeout for the long-running poll: if a worker dies, Temporal
    # waits at most this long before retrying the activity on another worker.
    qm_poll_heartbeat_timeout_seconds: float = Field(default=10.0, gt=0)
    # How often the poll loop heartbeats / re-checks the (mock) scheduler. Must be
    # positive — a zero interval would make the poll loop never advance.
    hpc_poll_interval_seconds: float = Field(default=2.0, gt=0)
    # Simulated submission latency and total run time of the mock HPC job.
    hpc_mock_submit_seconds: float = Field(default=1.0, gt=0)
    hpc_mock_run_seconds: float = Field(default=6.0, gt=0)
    # The real HPC execution path (plan F5): `hpc_launch_interface` selects the backend the QM
    # activities dispatch to — `"mock"` (default, the simulated SLURM spine kept for CI/local, needs
    # no cluster) or `"nextflow"` (the Seqera Platform/Tower REST launcher, ADR D-A5a). The
    # `hpc_api_*` values address and authenticate that launcher (the token arrives via the HPC
    # bridge / a mounted secret, F4-T6); `hpc_pipeline_name`/`_version` name the pipeline to run;
    # `hpc_artifact_store_url` is where a finished run's QM output blob is fetched from. All empty
    # in dev. `hpc_pipeline_version` also enters the calculation cache key *when set*, so a pipeline
    # bump is a cache miss not a stale hit (D-011/D-033) — while an empty version leaves the mock's
    # keys byte-identical to before F5.
    hpc_launch_interface: Literal["mock", "nextflow"] = "mock"
    hpc_api_base_url: str = ""
    hpc_api_token: str = ""
    hpc_pipeline_name: str = ""
    hpc_pipeline_version: str = ""
    hpc_artifact_store_url: str = ""
    # Real-run budgets for the `nextflow` poll (the mock uses `hpc_mock_run_seconds`). A real QM/DFT
    # run takes far longer than the mock: `hpc_run_timeout_seconds` is the poll activity's
    # single-attempt `start_to_close` cap (default 24h — heartbeating does NOT extend it, so it must
    # cover the whole run); `hpc_run_heartbeat_timeout_seconds` is the poll's heartbeat timeout, set
    # comfortably above one poll's HTTP round-trip + `hpc_poll_interval_seconds` so a slow launcher
    # does not trip a false dead-worker timeout; `hpc_http_timeout_seconds` bounds each launcher or
    # artifact HTTP call (a dedicated knob, not the Entra-token timeout).
    hpc_run_timeout_seconds: float = Field(default=86400.0, gt=0)
    hpc_run_heartbeat_timeout_seconds: float = Field(default=120.0, gt=0)
    hpc_http_timeout_seconds: float = Field(default=30.0, gt=0)
    # How many *consecutive* failed launcher polls (HTTP 5xx, transport blips) the poll activity
    # tolerates before failing its attempt. A transient blip during an up-to-24h run must not burn
    # the activity's shared retry budget — the loop just polls again next interval — while a
    # persistently broken launcher still surfaces within roughly this many poll intervals.
    hpc_poll_max_consecutive_errors: int = Field(default=30, ge=1)
    # Bearer token for the artifact store when it lives on a different origin than the launcher:
    # the launcher token must never be sent to a third host (F4 three-secret model). Empty means
    # the artifact fetch is unauthenticated — unless the store shares the launcher's origin, in
    # which case the launcher token still applies.
    hpc_artifact_store_token: str = ""
    # The HPC/Nextflow identity bridge (plan F4-T6, §7.2): the other non-Entra bridge. HPC is not an
    # Entra relying party, so user jobs run under one service identity while the requesting Entra
    # `oid` is carried in the payload (F4-T3) and *every* oid→HPC-identity mapping is logged for the
    # audit trail. This is the shared service identity jobs run as.
    hpc_bridge_identity: str = "chemclaw-hpc"

    @model_validator(mode="after")
    def _poll_faster_than_heartbeat(self) -> Self:
        """The poll loop must beat faster than Temporal's heartbeat timeout.

        Otherwise every `poll_hpc_status` activity is declared dead between two
        heartbeats and retried in a loop — a mis-set interval must fail at startup.
        The `nextflow` backend heartbeats on the same interval but against its own
        `hpc_run_heartbeat_timeout_seconds`, so that pair is checked when selected.
        """
        if self.hpc_poll_interval_seconds >= self.qm_poll_heartbeat_timeout_seconds:
            raise ValueError(
                "hpc_poll_interval_seconds must be smaller than qm_poll_heartbeat_timeout_seconds"
            )
        if (
            self.hpc_launch_interface == "nextflow"
            and self.hpc_poll_interval_seconds >= self.hpc_run_heartbeat_timeout_seconds
        ):
            raise ValueError(
                "hpc_poll_interval_seconds must be smaller than hpc_run_heartbeat_timeout_seconds "
                "when hpc_launch_interface='nextflow'"
            )
        return self

    @model_validator(mode="after")
    def _hpc_launch_config(self) -> Self:
        """`nextflow` needs the launcher endpoint, pipeline, and artifact store to be set.

        Checked at startup (mirroring `_llm_provider_config`) so a half-configured backend
        fails here with a clear message rather than as an opaque httpx protocol error five
        retried activity attempts deep in the first QM job. The `mock` dev path needs none.
        """
        if self.hpc_launch_interface == "nextflow":
            required = (
                ("hpc_api_base_url", self.hpc_api_base_url),
                ("hpc_pipeline_name", self.hpc_pipeline_name),
                ("hpc_artifact_store_url", self.hpc_artifact_store_url),
            )
            missing = [name for name, value in required if not value]
            if missing:
                raise ValueError(
                    f"hpc_launch_interface='nextflow' requires {', '.join(missing)} to be set"
                )
        return self


class CalculatorSettings(BaseSettings):
    """The fast local calculators: xTB, the pKa predictor, and the solubility model.

    Grouped because these knobs define the calculators' scientific parameters,
    and most of them enter the calculation cache key — changing one is a
    deliberate recompute, never a silent drift.
    """

    # xTB semiempirical calculator (plan step 1c.2). Method is the GFN parametrization
    # (latest: GFN2-xTB). `xtb_embed_seed` fixes RDKit 3D embedding so results are
    # reproducible; it is part of the cache key so changing it recomputes.
    xtb_method: str = "GFN2-xTB"
    xtb_embed_seed: int = 42

    # xTB-based pKa predictor (plan step 1c.4): pKa from the GFN2-xTB solvated
    # (ALPB) deprotonation energy via a linear calibration pKa = slope*dE + intercept.
    # Defaults fitted over 10 reference O-H acids (R^2 0.93, residual ~1.6 pKa units);
    # recalibrate against a proper dataset before production. Changing any of these
    # invalidates the cache (they are part of the key).
    pka_solvent: str = "water"
    pka_calibration_slope: float = 0.28733
    pka_calibration_intercept: float = -29.3116
    pka_uncertainty: float = 1.6
    # Reported log-S RMSE of the Reizman-descriptor solubility model (calc step 1c.3):
    # model uncertainty attached to every prediction, config like `pka_uncertainty`.
    solubility_rmse_log: float = 0.75


class BoSettings(BaseSettings):
    """Durable BoFire BO campaigns (plan step 1d.4).

    Grouped because these three knobs shape one thing: how a Bayesian-optimization
    campaign runs durably — its per-round activity budget, reproducibility seed,
    and the round ceiling that protects Temporal's event-history limit.
    """

    # A single round (BoFire propose + evaluate) can be slow, so activities get a
    # generous start-to-close budget.
    bo_activity_timeout_seconds: float = Field(default=300.0, gt=0)
    # Seed for BoFire's random design + SOBO strategies, so a campaign is reproducible
    # (deterministic seeding + proposals) rather than flaky run-to-run.
    bo_seed: int = 42
    # Ceiling on a campaign spec's round count. The observation history is carried as workflow
    # state and re-sent to the propose activity every round, so history bytes grow quadratically
    # with rounds and an unbounded spec would hit Temporal's hard event-history limit mid-run,
    # losing every already-paid evaluation. Generous versus the default of 10 rounds; a spec
    # beyond it is rejected at build time, not terminated by the server mid-campaign.
    bo_max_rounds: int = Field(default=500, ge=1)


class LlmSettings(BaseSettings):
    """The LLM provider seam (plan Phase F0) plus everything that rides its transport.

    Grouped because these knobs configure the one internal (or dev-Anthropic)
    endpoint and its uses: chat generation, per-task model routing (F10-E), the
    LLM-as-judge verifier (F10-B), and the embedding path (F10-A) — which reuses
    the LLM base_url/credential/TLS, so its provider knobs and the validator
    tying it to `llm_base_url` live here, in the section that owns that link.
    """

    # The agent's chat client is selected by config, so the deployment can point the agent at the
    # internal OpenAI-compatible ("OpenLLM-like") endpoint without any code change, keeping
    # Anthropic as a local-dev path. `openai_compatible` reaches the endpoint with **one generic
    # API credential** (`llm_api_key`) — deliberately *not* per-user Entra: the raw inference call
    # is not a user-scoped resource (see docs/foundation-plan.md §0). `llm_base_url`/`llm_model`
    # are required for `openai_compatible` (validated below); the TLS CA bundle, timeout, and
    # retry budget shape the transport so an internal endpoint with a private CA works from config
    # alone. `llm_temperature`/`llm_max_tokens` are the default generation params threaded into
    # the agent (F0.3). The default provider is `anthropic` so a fresh checkout config singleton
    # is valid with no endpoint set; production sets `CHEMCLAW_LLM_PROVIDER=openai_compatible` +
    # the base_url/model. No provider client class is imported outside `agents/llm_provider.py`.
    llm_provider: Literal["openai_compatible", "anthropic"] = "anthropic"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_tls_ca_bundle: str = ""
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=3, ge=0)
    llm_temperature: float = Field(default=0.0, ge=0)
    llm_max_tokens: int = Field(default=4096, gt=0)
    # Per-task model routing (plan F10-E). Maps a task name to the model id to use for it, so a
    # cheap model can run high-throughput/secondary steps (verification, classification) while the
    # frontier model drives the main reasoning turn — without a second provider or a second import
    # site (`build_chat_client(task)` stays the one place a client is built). Model ids are for the
    # *active* provider (an `openai_compatible` model name, or an Anthropic one); a task with no
    # entry falls back to the provider's default (`llm_model`/`agent_model`), so an empty map (the
    # default) is exactly today's single-model behavior. ENV override is JSON, e.g.
    # CHEMCLAW_MODEL_ROUTES='{"verifier": "internal-small", "agent": "internal-large"}'.
    model_routes: dict[str, str] = Field(default_factory=dict)
    # Answer verification & confidence routing (plan F10-B). When `verifier_enabled`, a drafted
    # answer is checked for citation faithfulness by an LLM-as-judge on the cheap routed model
    # (task `"verifier"`, F10-E): each factual claim is scored against the evidence it cites, and an
    # aggregate `confidence` in [0,1] is returned. An answer scoring below
    # `verifier_confidence_threshold` is flagged for human review (the confidence + the unsupported
    # claims ride on the turn's `AnswerEvent`), reusing the existing D-032 hold — no new gate. When
    # disabled (the default), the verifier falls back to the deterministic report citation check
    # (`report.harness.verify_claims`) so there is no network dependency and no behavior change.
    verifier_enabled: bool = False
    verifier_confidence_threshold: float = Field(default=0.7, ge=0, le=1)
    # Embedding provider (plan F10-A). Selects how a note/query is embedded: `hash` is a
    # deterministic, offline, dependency-free feature-hash (dev/CI only — token-overlap
    # similarity, NOT neural-semantic); `openai_compatible` calls the internal endpoint's
    # `/embeddings` route (`embedding_model`), reusing the LLM base_url/credential/TLS transport.
    # `embedding_dim` must match both the model's output width and the `note_index.embedding`
    # column (`vector(N)` in infra/sql/012) — changing it is a new migration, like the fingerprint
    # bit width.
    embedding_provider: Literal["hash", "openai_compatible"] = "hash"
    embedding_model: str = ""
    embedding_dim: int = Field(default=1536, gt=0)

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


class AgentSettings(BaseSettings):
    """The MAF conversational agent: model, skills, capabilities, compaction, harness.

    Grouped because everything here shapes how `build_agent` assembles one agent —
    which model orchestrates, which skills and MCP capability servers attach,
    how the conversation context is compacted, and whether the autonomous
    plan/execute harness (Phase F1) wraps it.
    """

    # MAF agent (plan step 1.5). `agent_model` is the orchestration model name
    # (ENV-overridable); the provider's API key is read by the chat client from
    # its own env var (e.g. ANTHROPIC_API_KEY), not stored here. `skills_dir` is
    # where the agent discovers SKILL.md files — one or more directories, delimited by the
    # OS path separator (like PATH), so an admin can add a second (e.g. team-private) skills
    # directory without code changes. Read it through the `skills_dirs` property, never raw.
    agent_model: str = "claude-sonnet-5"
    skills_dir: str = "skills"
    # Role-scoped skill visibility (plan step 6.2): map a skill name to the Entra app-roles allowed
    # to see it. A skill not listed is ungated (advertised to everyone); a listed skill is hidden
    # from a caller (the turn's ambient identity) holding none of its roles. Empty default = every
    # skill visible (today's behavior). ENV override is JSON, e.g.
    # CHEMCLAW_SKILL_ROLE_GATES='{"deep-research": ["process-chemist"]}'.
    skill_role_gates: dict[str, list[str]] = Field(default_factory=dict)
    # MCP capability servers the agent attaches over stdio (the plan's capability layer, D-029):
    # the agent calls the fingerprint search over the MCP protocol rather than importing it
    # in-process, so a capability is a running server, not agent code. Adding one is an entry
    # here (ENV-overridable as JSON), never a change to `build_agent`. Each runs as its own
    # subprocess launched from the repo root; `allowed_tools` keeps the agent to the read/search
    # tools (index/write stays in the PR-gated ingestion path, off the conversational agent).
    mcp_servers: list[McpServerSpec] = [
        McpServerSpec(
            name="mcp-molfp",
            command=sys.executable,
            args=["-m", "mcp_servers.molfp.server"],
            allowed_tools=["similar_molecules", "substructure_matches"],
        ),
        McpServerSpec(
            name="mcp-rxnfp",
            command=sys.executable,
            args=["-m", "mcp_servers.rxnfp.server"],
            allowed_tools=["similar_reactions"],
        ),
    ]
    # Conversation context management (MAF compaction). The agent keeps a session thread and
    # composes tool calls that return large payloads (evidence sweeps, full ELN recipes), so a
    # long chat would grow unbounded. Compaction runs only when the included context exceeds
    # `agent_context_token_budget` (measured with a char/4 estimator — no external tokenizer),
    # then reclaims tokens cheapest-first: collapse stale tool-result dumps to a short trace
    # (keeping the newest `agent_keep_last_tool_groups` verbatim), then drop older conversation
    # turns beyond `agent_keep_last_conversation_groups`. System instructions/skills are always
    # kept. No LLM summarizer — deterministic and credential-free.
    agent_context_token_budget: int = Field(default=100_000, ge=1)
    agent_keep_last_tool_groups: int = Field(default=2, ge=0)
    agent_keep_last_conversation_groups: int = Field(default=12, ge=1)

    # Local testing CLI (`agents.cli`). The CLI is a developer affordance for driving the agent
    # from a terminal; the production ingress is Teams/Copilot with native Entra-ID SSO
    # (architektur.md §7), not this. Because Entra enforcement defaults off in dev
    # (`entra_required=False`), the CLI can only run in explicit `--admin` mode, which bypasses
    # auth for testing and attributes the audit trail to this actor. It is a config value (not a
    # hardcoded string) so a deployment can label its test runs — e.g. a machine name — rather
    # than a generic "admin".
    cli_admin_actor: str = "admin@localhost"

    # MAF Agent Harness (plan Phase F1) — the autonomous plan/execute backbone (the Claude-Code-like
    # experience). When `harness_enabled`, `build_agent` wires MAF's `create_harness_agent` (todo
    # list + plan/execute mode + a bounded completion loop) over the *same* tools/skills/audit/
    # compaction as the classic agent, with MAF's generic batteries (file memory/access, web search,
    # shell) OFF — capability comes from our MCP servers and tools, not the harness's built-ins. Off
    # by default so today's classic single-turn agent stays the safe fallback against the harness's
    # `[Experimental]` API. `harness_autonomy` picks the starting mode: `plan_only` (default, the
    # pharma-safe one) starts in plan mode and presents a plan for human approval before any
    # execution — the pre-execution GxP gate — and only loops once approval switches it to execute;
    # `execute` starts looping through the todo list immediately. `harness_max_loop_iterations` caps
    # the loop so a stuck plan aborts instead of spinning (the runaway guard).
    harness_enabled: bool = False
    harness_autonomy: Literal["plan_only", "execute"] = "plan_only"
    harness_max_loop_iterations: int = Field(default=25, ge=1)

    @property
    def skills_dirs(self) -> list[str]:
        """The skills directories, split on the OS path separator (like PATH), empties dropped.

        `FileSkillsSource` takes a list of directories; keeping the config a single delimited
        string (rather than a JSON list) means an admin sets `CHEMCLAW_SKILLS_DIR=skills:/opt/
        team-skills` the same way they set `PATH`, no JSON quoting.
        """
        return [d for d in self.skills_dir.split(os.pathsep) if d]


class ServiceSettings(BaseSettings):
    """The front-door run service (plan Phase F2/F3): binding, limits, sessions, budgets.

    Grouped because these knobs all guard the one ASGI trust boundary: how the
    server binds, what a request may cost (size, concurrency, wall-clock, token
    budgets), and how durable sessions + job push-back reach the browser.
    """

    # The ASGI service that actually *runs* the agent for a chemist: it builds the agent, opens
    # the MCP tool lifecycle for the turn, streams the response, and serves the browser chat
    # surface. `service_host`/`service_port` bind the server (the OpenShift Route front-ends it,
    # F6). `service_cors_origins` is a comma-separated allow-list for browser origins that may
    # call the API (empty = none, the safe default; a same-origin embedded UI needs none). These
    # are the only front-door knobs; identity/OIDC is layered on in F4.
    # Binds all interfaces inside the container; the OpenShift Route + NetworkPolicy gate ingress.
    service_host: str = "0.0.0.0"
    service_port: int = Field(default=8080, gt=0)
    # Explicit opt-in to boot *unauthenticated on a non-loopback bind* (SEC-2). With
    # `entra_required` False every request runs as the shared dev principal with all authorization
    # gates open — safe only behind loopback. The front door refuses to start in that mode on an
    # exposed interface unless this is set, so an exposed unauthenticated deployment is a conscious
    # decision (one loud env var), never a default. Loopback dev and Entra-enforced deployments
    # never need it.
    service_allow_insecure: bool = False
    service_cors_origins: str = ""
    # Max characters accepted in one chat message at the front door (SEC-4). Bounds the request body
    # at the trust boundary so an oversized POST is a clean 422, not an unbounded allocation.
    # Generous for a real message (~25k tokens); raise it for a workflow that posts more.
    service_max_message_chars: int = Field(default=100_000, gt=0)
    # Response security headers on the browser surface (SEC-5). When on (the safe default), every
    # response carries a Content-Security-Policy scoped to the self-served chat UI (self + one
    # inline <style> block + data: images), X-Content-Type-Options: nosniff, X-Frame-Options: DENY,
    # and Strict-Transport-Security. Off is only for a deployment fronting its own header policy at
    # the ingress/Route. HSTS is inert over plain-HTTP dev, so leaving this on locally is harmless.
    service_security_headers: bool = True

    # Durable session store (plan Phase F3). The agent's conversation history must survive a pod
    # restart, so a session is resumable. `memory` keeps the classic in-process provider (dev/test);
    # `postgres` persists each turn's messages to `session_messages` keyed by session id, so a fresh
    # process over the same DSN resumes the thread. **Session state is not Temporal job state** — it
    # is the conversation layer (D-002), and compaction still runs on top. `session_store_dsn` lets
    # the session store point at a different database than the calculation/fingerprint DSN; empty
    # falls back to `postgres_dsn` (one database in the simple deployment).
    session_store: Literal["memory", "postgres"] = "memory"
    session_store_dsn: str = ""
    # Cap on the front door's in-process live-session cache (COR-3). The service holds the live
    # AgentSession object per session id; without a bound this map grows for the pod's whole
    # lifetime. When the cap is exceeded the least-recently-used session is evicted — its durable
    # history survives in the session store, only the in-process handle is dropped. Sized generously
    # for concurrent chemists; raise it for a busier front door.
    service_max_live_sessions: int = Field(default=1000, gt=0)
    # Admission control on concurrent agent turns (AG-15). Each turn holds one permit for its whole
    # streamed run, so at most this many turns hit the shared internal LLM endpoint at once; a turn
    # that cannot get a permit within the admission timeout is shed with 503 (retry) rather than
    # piling onto a saturated endpoint. Tune to the endpoint's real throughput budget — the default
    # is deliberately conservative. Health and push-back streams are not gated (they are not
    # LLM-bound).
    service_max_concurrent_turns: int = Field(default=8, gt=0)
    service_turn_admission_timeout_seconds: float = Field(default=5.0, gt=0)
    # Wall-clock bound on one streamed turn — how long a turn may hold its admission permit. The
    # admission timeout only bounds the *wait* for a permit; without this, a hung model stream or a
    # deliberately slow-reading SSE client pins a permit indefinitely, and a handful of such streams
    # collapses the whole front door's capacity (every other turn is shed 503). On expiry the client
    # gets one user-safe error event and the permit is released. Generous for a real turn (an async
    # QM job is submitted, not awaited, within the turn), finite against a stall.
    service_turn_timeout_seconds: float = Field(default=600.0, gt=0)
    # Turn/token budgets — the runaway-cost guard (service.budget). A single turn is already
    # iteration-capped (`harness_max_loop_iterations` / MAF's 40), but nothing caps the *number* of
    # turns, so a client or an automated push-back loop could accumulate unbounded LLM spend. When
    # `budget_enabled`, the front door meters each turn's reported token usage and counts turns per
    # session and per user, refusing (HTTP 429) a turn that would exceed a cap. Caps are per running
    # process and best-effort — they reset on restart, bounding a live process's runaway (the
    # missing ceiling above the per-turn loop cap), not a durable rolling-window quota (deferred).
    # A cap of 0 means unlimited on that dimension, so a deployment can enable just the guard it
    # wants; the defaults are generous for a real chemist but finite against a loop. Token metering
    # reads MAF's usage content, so a provider reporting no usage meters 0 and the turn caps bind.
    # Off by default (today's behavior).
    budget_enabled: bool = False
    budget_max_turns_per_session: int = Field(default=100, ge=0)
    budget_max_tokens_per_session: int = Field(default=2_000_000, ge=0)
    budget_max_turns_per_user: int = Field(default=1000, ge=0)
    budget_max_tokens_per_user: int = Field(default=20_000_000, ge=0)
    # Cap on distinct users the in-process budget tracker keeps counters for. The tracker lives for
    # the pod's lifetime, so without a bound its per-user map grows with every principal ever seen
    # (a slow leak); past the cap the least-recently-active user's counters are evicted (reset) —
    # acceptable for a best-effort guard whose durable rolling-window quota is a conscious deferral.
    # The per-session map is bounded by `service_max_live_sessions` (the session lifecycle bound).
    budget_max_tracked_users: int = Field(default=10_000, gt=0)
    # Job→session push-back (plan F3-T2/T3): a finished Temporal job writes a `session_events` row;
    # the front door tails the table and wakes the owning session (appending the result, flipping
    # the `awaiting` todo) instead of the user polling. This is the tailer's poll interval — a
    # LISTEN/NOTIFY-free fallback that is simple and correct; lower it for snappier wake-ups.
    session_event_poll_seconds: float = Field(default=2.0, gt=0)
    # Cap on concurrent push-back event streams (`GET /sessions/{id}/events`) per user. The turn
    # semaphore only guards POSTed turns; each event stream polls the database for its whole
    # lifetime, so without a bound one user (or a pile of abandoned tabs) can accumulate hundreds of
    # forever-polling streams and exhaust Postgres connections for everyone. A real client needs one
    # stream per open session view; past the cap the request is refused with 429.
    service_max_event_streams_per_user: int = Field(default=5, gt=0)


class EntraSettings(BaseSettings):
    """Azure Entra ID identity and authorization (plan Phase F4, F10-C).

    Grouped because identity is one coherent contract: the OIDC fields, the
    derived JWKS/issuer URLs, the parsed role/action sets, the tool-authz gates,
    the workload-federation/OBO bridges, and the enforcement validator that
    rejects a half-configured deployment — all in one place (kernel review note).
    """

    # User auth at the front door is OIDC with Entra as the IdP: the service is an Entra app
    # registration, and every non-health request carries an Entra JWT that is validated against
    # the tenant JWKS with the audience checked (the confused-deputy guard — the service is both
    # OAuth client and resource). `oid`/`upn` + app-roles are extracted into a `Principal` that
    # authorizes and attributes every backend action. `entra_required` gates enforcement: True in
    # any real deployment (a missing/invalid token is 401); False only for local dev, where a
    # stand-in principal runs the app without a tenant. `entra_jwks_url`/`entra_issuer` default
    # empty and derive from `entra_tenant_id` when set (the standard v2.0 endpoints), so a
    # deployment sets just tenant + audience + required.
    entra_required: bool = False
    entra_tenant_id: str = ""
    entra_audience: str = ""
    entra_jwks_url: str = ""
    entra_issuer: str = ""
    # Authorization for expensive triggers (plan F4-T5): the single fachliche gate. An action named
    # in `entra_expensive_actions` (comma list, e.g. "submit_qm_job,start_bo_campaign") may run only
    # for a user holding at least one role in `entra_privileged_roles` — so an autonomously-planned
    # todo cannot launch a costly HPC/BO job outside the requesting user's entitlements. Enforced
    # only when `entra_required` (a real deployment with real roles); in dev the gate is open. Both
    # empty by default: nothing is privileged until a deployment declares it.
    entra_expensive_actions: str = ""
    entra_privileged_roles: str = ""
    # Per-tool authorization (plan F10-C): generalizes the single expensive-trigger gate to *every*
    # tool invocation via one middleware. `tool_role_gates` maps a tool name to the Entra app-roles
    # allowed to call it. A tool with no entry follows `tool_authz_default`: under `"deny"`
    # (allowlist mode) it is refused outright — only listed tools are callable, by a role-holder;
    # under `"allow"` it is callable, except the built-in write-tool gates
    # (`agents.authz.DEFAULT_WRITE_TOOL_GATES`: job launchers and state-mutating tools require an
    # `entra_privileged_roles` role out of the box — an explicit entry here overrides that). The
    # built-in write gate only narrows `"allow"`; it never widens `"deny"`. Enforced only when
    # `entra_required` (dev gate is open).
    # ENV override for the gates is JSON, e.g. CHEMCLAW_TOOL_ROLE_GATES='{"submit_qm_job":
    # ["process-chemist"]}'. Note: `deny` with an empty `tool_role_gates` blocks *all* tools — a
    # deliberate lockdown, not a footgun to stumble into.
    tool_role_gates: dict[str, list[str]] = Field(default_factory=dict)
    tool_authz_default: Literal["allow", "deny"] = "allow"
    # The identity a *user-triggered* workflow records when there is no authenticated user
    # (plan F4-T3). Only reachable in local dev (`entra_required=False`, no tenant) and for
    # system-triggered jobs; under enforcement `require_actor` rejects an absent user instead
    # of falling back. Config, not the old magic `"unknown"` literal.
    service_actor_id: str = "service-account"
    # Workload identity federation (plan F4-T2): a backend pod mints its *own* short-lived Entra
    # token by exchanging its projected ServiceAccount JWT (at `entra_sa_token_path`) via the OAuth2
    # client-credentials grant with a `client_assertion` — no client secret ever at rest. Disabled
    # by default (local dev has no tenant). The generic LLM credential is the documented exception
    # and does NOT use this path. `entra_token_refresh_leeway_seconds` refreshes a cached token
    # before it actually expires; `entra_http_timeout_seconds` bounds the token/OBO HTTP calls.
    entra_workload_federation_enabled: bool = False
    entra_workload_client_id: str = ""
    entra_token_endpoint: str = ""
    entra_sa_token_path: str = "/var/run/secrets/azure/tokens/azure-identity-token"
    entra_token_refresh_leeway_seconds: float = Field(default=300.0, gt=0)
    entra_http_timeout_seconds: float = Field(default=10.0, gt=0)
    # On-Behalf-Of exchange (plan F4-T4): when a backend acts for a specific user against a
    # user-scoped resource (ELN/LIMS), it swaps the user's token OBO for a downstream token so the
    # resource sees the real user, not the service. Generic and dormant — off until a user-scoped
    # source (the deferred custom Snowflake ELN connector) opts in by calling `exchange_obo`.
    entra_obo_enabled: bool = False

    @property
    def entra_expensive_action_set(self) -> frozenset[str]:
        """The actions that require a privileged role (parsed comma list)."""
        return frozenset(a.strip() for a in self.entra_expensive_actions.split(",") if a.strip())

    @property
    def entra_privileged_role_set(self) -> frozenset[str]:
        """The roles that authorize an expensive action (parsed comma list)."""
        return frozenset(r.strip() for r in self.entra_privileged_roles.split(",") if r.strip())

    @property
    def entra_jwks_endpoint(self) -> str:
        """The JWKS URL: explicit override, else the tenant's standard v2.0 keys endpoint."""
        if self.entra_jwks_url:
            return self.entra_jwks_url
        return f"https://login.microsoftonline.com/{self.entra_tenant_id}/discovery/v2.0/keys"

    @property
    def entra_issuer_url(self) -> str:
        """The token issuer: explicit override, else the tenant's standard v2.0 issuer."""
        if self.entra_issuer:
            return self.entra_issuer
        return f"https://login.microsoftonline.com/{self.entra_tenant_id}/v2.0"

    @model_validator(mode="after")
    def _entra_enforcement_is_configured(self) -> Self:
        """Under `entra_required`, fail fast on a half-configured identity setup (review finding).

        Two footguns the front-door/authorization code cannot catch at request time:
        - an empty `entra_audience` (or no tenant/issuer/JWKS) makes every token rejected — a
          deny-all availability outage that should surface at startup, not as mysterious 401s.
          The issuer and the JWKS endpoint derive independently from the tenant, so each needs
          its own source: an issuer alone cannot resolve the keys endpoint;
        - declaring privileged roles *or* expensive actions but not the other leaves the role gate
          silently open (an action with no expensive-set entry authorizes every user), so the two
          must be set together — set neither to deliberately gate nothing.
        """
        if not self.entra_required:
            return self
        if not self.entra_audience:
            raise ValueError("entra_audience must be set when entra_required")
        if not (self.entra_tenant_id or self.entra_issuer):
            raise ValueError("entra_tenant_id or entra_issuer must be set when entra_required")
        if not (self.entra_tenant_id or self.entra_jwks_url):
            raise ValueError(
                "entra_tenant_id or entra_jwks_url must be set when entra_required "
                "(the issuer alone cannot resolve the JWKS keys endpoint)"
            )
        if bool(self.entra_expensive_actions) != bool(self.entra_privileged_roles):
            raise ValueError(
                "entra_expensive_actions and entra_privileged_roles must be set together "
                "(the role gate is silently open otherwise)"
            )
        return self


class KgSettings(BaseSettings):
    """The Markdown knowledge graph and its PR-gate (plan Phase 2).

    Grouped because these knobs describe the one Git-backed note repository:
    where notes live, how the GitNoteSubmitter branches/pushes them through the
    PR-gate, and how long a human-approval hold may pend.
    """

    # Directory of note files the indexer reads; retrieval is graph traversal
    # over their [[wikilinks]] (D-004).
    knowledge_dir: str = "knowledge"
    # Upper bound on `expand_note`'s link-expansion depth (SEC-4). The tool takes `hops` from the
    # model; an unbounded value would traverse the whole graph. 1–2 is typical; clamp to this so a
    # large value is bounded rather than rejected.
    graph_max_hops: int = Field(default=3, ge=1)
    # PR-gate git settings (plan steps 2.7, 2.8): agent notes branch off this base
    # branch on this remote before a human merges.
    note_base_branch: str = "main"
    git_remote: str = "origin"
    # The checkout the GitNoteSubmitter mutates (`git checkout -B` switches its whole
    # working tree). Point it at a dedicated clone of the knowledge repo in production;
    # the "." default only suits a dev checkout with nothing else running in it.
    note_repo_dir: str = "."
    # Publishing a QM result as a graph note is best-effort: bounded attempts + its
    # own timeout so a persistent failure gives up instead of retrying forever.
    note_write_timeout_seconds: float = Field(default=120.0, gt=0)
    note_write_max_attempts: int = Field(default=3, ge=1)
    # Wall-clock bound on a single git command in the PR-gate submitter. A hung
    # fetch/push (dead remote, credential prompt) is killed after this, so it can
    # never deadlock the process-wide submit lock; the failed activity then retries.
    git_command_timeout_seconds: float = Field(default=60.0, gt=0)

    # How long a confirmed-answer note is held pending a human Yes/No before the
    # hold expires unpublished (plan step 5.5, async approval seam). The button click
    # is a Temporal signal into `InteractionApprovalWorkflow`; this bounds the wait so
    # an unanswered prompt cannot pin a workflow forever. Default 7 days — generous for
    # an out-of-band review, still finite.
    interaction_approval_timeout_seconds: float = Field(default=604800.0, gt=0)

    @model_validator(mode="after")
    def _knowledge_dir_is_relative(self) -> Self:
        """`knowledge_dir` must be relative to the note repo, never an absolute path.

        The PR-gate builds a note path as `Path(note_repo_dir) / knowledge_dir / …`. An
        absolute `knowledge_dir` would make `Path.__truediv__` discard `note_repo_dir`,
        so the write would land outside the repo — the containment check then fails the
        submit, confusingly. Reject it at startup where the message is clear instead.
        """
        if os.path.isabs(self.knowledge_dir):
            raise ValueError(
                f"knowledge_dir must be relative to note_repo_dir, "
                f"got absolute {self.knowledge_dir!r}"
            )
        return self


class EvalSettings(BaseSettings):
    """The evaluation & metric layer (plan Phase 2b, F10-F2).

    Grouped because a metric's pass/fail threshold is config, never hardcoded
    (G3): the case-set locations, the green-chemistry gates, the A/B noise
    floor, the drift job, and the retrieval-quality gate all live here.
    """

    # A metric is a pure function; the green-chemistry limits are dimensionless (kg waste or
    # input per kg product) and process-dependent — these defaults are lenient gate values,
    # tune them per chemistry.
    # Versioned eval case-set. Its own directory, not under `knowledge_dir`: an eval
    # case is a structured evaluation payload (output/reference), not a relational
    # note, so it neither uses the note schema nor passes through kg-validate.
    eval_case_dir: str = "evals/cases"
    eval_efactor_max: float = 50.0
    eval_pmi_max: float = 50.0
    # Absolute error (in the prediction's own unit, e.g. log S) still counted as an
    # accurate prediction against a held-out reference.
    eval_prediction_tolerance: float = 1.0
    # Noise floor for the per-task tool-utility A/B (plan step 2b.4): a metric delta
    # within +/- this magnitude counts as "no effect", so tool augmentation is only
    # credited (or blamed) for changes above measurement noise. One global scalar —
    # a comparison does not know which metric produced its scores, so set it to the
    # noisiest metric's floor (per-metric floors need a per-metric parameter first).
    # The default is a small floating-point floor so runs differing only by rounding
    # register as "no effect" (a 0.0 default made *every* non-exact-tie helped/hurt,
    # defeating the band); raise it to the actual measurement noise of the metric a
    # given case-set exercises.
    eval_ab_epsilon: float = Field(default=1e-6, ge=0.0)
    # Eval drift detection (plan F10-F2). A `background-jobs` workflow re-runs the committed
    # case-set on a cadence and alerts when an aggregate metric moves further than a *relative* band
    # (`eval_drift_epsilon` × the baseline value) from the Git-committed baseline
    # (`evals/baseline.json`). Relative, so one knob is scale-appropriate across metrics of
    # different magnitudes (an `f1` in [0, 1] vs an `e_factor` near 35); 0.05 = a 5% proportional
    # move. Off by default; enabling it adds the Schedule (D-035).
    eval_drift_enabled: bool = False
    eval_drift_schedule_minutes: int = Field(default=1440, ge=1)
    eval_drift_epsilon: float = Field(default=0.05, ge=0)
    # The drift-check activity's own timeout (not borrowed from the memory job's): five pinned cases
    # score in well under this, but a dedicated knob keeps the two jobs' timeouts independent.
    eval_drift_timeout_seconds: float = Field(default=300.0, gt=0)
    eval_baseline_path: str = "evals/baseline.json"
    # Retrieval-quality gate (audit KM-13). A gold query→expected-source set scores
    # `GraphRetriever` over this fixed corpus fixture (a small versioned set of notes, NOT the
    # live `knowledge_dir`, so the score is reproducible). `retrieval_recall_min` is the floor
    # the "did we surface the expected evidence?" recall metric gates against — the seam that
    # catches a substring-filter or evidence-cap change quietly dropping recall.
    eval_retrieval_corpus_dir: str = "evals/retrieval_corpus"
    retrieval_recall_min: float = Field(default=0.75, ge=0.0, le=1.0)


class FingerprintSettings(BaseSettings):
    """Molecule/reaction fingerprint search (plan Phase 3, mcp-molfp/mcp-rxnfp).

    Grouped because the fingerprint definition (and thus the stored column
    width) is a deliberate, versioned choice, and the search bounds guard the
    same SQL/RDKit paths those definitions feed.
    """

    # ECFP4 = Morgan radius 2, 2048 bits; both are config so the fingerprint definition is a
    # deliberate choice, not a magic number. The similarity threshold is the Tanimoto floor a
    # match must clear to count as a structural neighbor — the capability exposes it, the
    # `reaction-search` skill decides how to wield it (G6).
    ecfp_radius: int = Field(default=2, ge=0)
    ecfp_bits: int = Field(default=2048, gt=0)
    # DRFP reaction fingerprint width (plan step 3.4, mcp-rxnfp). Its own field, not shared
    # with ecfp_bits — a different fingerprint whose folded length is an independent choice,
    # though both default to 2048 (matching their bit(N) columns). top_k/threshold below are
    # shared: they are generic fingerprint-search knobs, not molecule-specific.
    drfp_bits: int = Field(default=2048, gt=0)
    fingerprint_top_k: int = Field(default=10, ge=1)
    fingerprint_similarity_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    # Upper bound on an agent-supplied `top_k` for the similarity tools (SEC-4). `top_k` reaches
    # `find_matches` from the model (agents.search_tools) and lands directly in a SQL `LIMIT`, so an
    # arbitrarily large value would be an unbounded query. Clamp it to this — the fingerprint-search
    # analog of the `graph_max_hops` clamp on `expand_note`. Generous for a real neighbor list.
    fingerprint_max_top_k: int = Field(default=100, ge=1)
    # Bound on how many stored fingerprints one substructure scan materializes (SEC-4). The scan
    # has no similarity prefilter, so it loads records and RDKit-matches each; without a cap a
    # large corpus is a full-table load into the worker heap (the 30s statement_timeout bounds DB
    # time, not rows returned). The scan takes at most this many rows (deterministic id order) and
    # logs a warning when it hits the cap so a truncated result is never silent. Raise it for a
    # larger corpus, or add a pattern-fingerprint prefilter (deferred) when it starts truncating.
    substructure_scan_max_records: int = Field(default=5000, gt=0)
    # Bound on the length of a model-supplied substructure query string (SEC-4). SMARTS
    # matching is subgraph isomorphism (worst-case exponential) run in-process over the
    # scanned corpus with no statement_timeout analog, so a pathological multi-KB pattern
    # could pin the server. Real pharmacophore/functional-group SMARTS run tens to a few
    # hundred characters; 500 leaves generous headroom while rejecting degenerate input.
    substructure_query_max_length: int = Field(default=500, gt=0)


class ElnSettings(BaseSettings):
    """ELN ingestion (plan Phase 4): the export adapters and the durable sync loop.

    Grouped because these knobs shape one ingestion pipeline: where the JSON/ORD
    exports land, how the cursor-driven sync batches/overlaps/heartbeats, and
    how often its Temporal Schedule fires. ELN-specific format lives only in the
    adapter, never in config (G6).
    """

    # The one concrete adapter reads a JSON-export ELN from this directory; the sync activity's
    # timeout bounds one batch of fetch+validate+index+PR-gate work.
    eln_export_dir: str = "eln/exports"
    eln_sync_timeout_seconds: float = Field(default=300.0, gt=0)
    # The sync fetches from this far *behind* its high-water cursor, so an export file that
    # lands late with an older payload timestamp (an upstream export-job retry) is still
    # picked up instead of being silently dropped forever. Re-fetching the window is safe
    # and cheap because ingestion is idempotent; one day covers routine export retries —
    # anything later needs a manual backfill (explicit `since`).
    eln_sync_overlap_seconds: float = Field(default=86400.0, ge=0)
    # An entry stamped further than this beyond the wall clock is rejected, not ingested: a
    # typo'd future year would otherwise become the persisted high-water cursor and silently
    # skip every later real entry (no code path ever lowers a stored cursor). One day
    # tolerates clock skew and timezone mishaps while catching implausible timestamps.
    eln_sync_future_tolerance_seconds: float = Field(default=86400.0, ge=0)
    # Bounds one sync activity attempt's *new* work: at most this many entries newer than the
    # cursor are ingested per attempt, and the workflow loops chunk by chunk, persisting the
    # advanced cursor after each one — so an arbitrarily large backlog makes bounded forward
    # progress instead of timing out one giant attempt forever. Entries inside the overlap
    # window re-ingest idempotently and do not count against the bound. Sized so a full chunk
    # of per-entry PR-gate pushes fits comfortably inside `eln_sync_timeout_seconds`.
    eln_sync_batch_size: int = Field(default=100, ge=1)
    # Dead-worker detection for the (long-running) sync activity: it heartbeats while it
    # ingests, so Temporal notices a dead worker within this window instead of waiting out
    # the whole `eln_sync_timeout_seconds` start-to-close before retrying elsewhere.
    eln_sync_heartbeat_timeout_seconds: float = Field(default=60.0, gt=0)
    # A second concrete adapter reads native Open Reaction Database messages (human-readable
    # ORD JSON) from this directory — the "structured recipe" path, alongside the free-text
    # JSON export above. Same `ElnAdapter` contract, so both flow through the one sync loop.
    ord_export_dir: str = "eln/exports/ord"
    # Temporal Schedule cadence for the ELN sync (`scripts/schedules.py`, applied by
    # `make schedules-apply`). The sync is self-cursoring (loads/stores its high-water mark in
    # `sync_cursors`), so its Schedule passes no argument. Schedules live in Temporal
    # (durability there, not host cron); overridable so a deployment tunes cadence without
    # code change.
    eln_sync_schedule_minutes: float = Field(default=60.0, gt=0)


class SourcesSettings(BaseSettings):
    """The generic `DataSource` seam (plan F7): which registered sources are active.

    Its own section because the seam is deliberately source-agnostic — adding a
    source (first live one: a custom Snowflake ELN connector) is one registry
    entry + one key here, zero core edits — so it belongs to neither the ELN
    section nor the retrieval section alone.
    """

    # A comma list of `sources.registry` keys. `graph` is the knowledge-graph retriever
    # (retrieve-only); `eln-json`/`eln-ord` re-host the ELN adapters (ingest-only).
    # `active_retrieve_sources()` feeds `gather_evidence`, so the default keeps today's
    # exactly-one-graph-retriever behavior; `active_ingest_sources()` feeds the ELN sync,
    # defaulting to the JSON adapter as before.
    data_sources: str = "graph,eln-json"

    @property
    def data_source_list(self) -> list[str]:
        """The active data-source keys, parsed from the comma list (order kept, blanks dropped)."""
        return [s.strip() for s in self.data_sources.split(",") if s.strip()]


class MemorySettings(BaseSettings):
    """The memory layers (plan Phase 5): playbook and campaign synthesis.

    Grouped because these thresholds define what the semantic/episodic layers
    may claim ("same transformation" vs "related chemistry"), plus the synthesis
    jobs' timeout and Schedule cadence.
    """

    # The semantic layer distils a playbook only from reactions whose DRFP similarity clears
    # this floor and that recur across >=2 projects — higher than the search floor, since a
    # playbook claims "same transformation", not just "related".
    playbook_similarity_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    # The episodic layer groups an *optimization campaign* — repeated runs of the **same
    # transformation** (a screen varying conditions/reagents) — by DRFP similarity. Higher than
    # the playbook floor: an optimization series is the same reaction re-run, not merely related
    # chemistry, so the grouping must be tight to avoid merging distinct transformations.
    optimization_similarity_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    memory_job_timeout_seconds: float = Field(default=300.0, gt=0)
    # Temporal Schedule cadence for the memory-synthesis jobs (`scripts/schedules.py`): they
    # re-scan the whole corpus, so they run less often than the cursor-driven ELN sync.
    memory_synthesis_schedule_minutes: float = Field(default=1440.0, gt=0)


class RetrievalSettings(BaseSettings):
    """Evidence retrieval (plan F10-A + the gather_evidence sweep budgets).

    Grouped because these knobs tune how evidence reaches the agent: the hybrid
    (dense/lexical) retrievers' bounds and fusion mode, the sweep's chunk cap
    and rank-before-truncate scoring, the shared note-excerpt budget, and the
    parsed-graph cache. The embedding *provider* knobs live in the LLM section
    (they ride the LLM transport); these are the retrieval-behavior knobs.
    """

    # Dense-embedding and lexical (Postgres FTS) retrievers complement the graph/fingerprint
    # search as *entry points* into graph traversal (D-004: the git-markdown graph stays the
    # source of truth, embeddings are a derived index). They attach through the F7 data-source
    # registry (`data_sources`), so the enable switch is registry membership, not a second
    # boolean. `retrieval_top_k` bounds each new retriever's hits. `retrieval_mode` picks how
    # `gather_evidence` combines sources: `graph` (default) keeps today's flat union + dedup;
    # `hybrid` fuses the per-source rankings by Reciprocal Rank Fusion (`retrieval_fusion_k` is
    # the RRF constant) so a note surfaced by any single source rises, then graph expansion
    # (expand_note) remains the reasoning path.
    retrieval_top_k: int = Field(default=8, gt=0)
    retrieval_mode: Literal["graph", "hybrid"] = "graph"
    retrieval_fusion_k: int = Field(default=60, gt=0)
    # How much of a source note's body an excerpt carries — shared by the report harness's
    # evidence excerpts and the memory layer's procedure excerpts (one note-excerpt budget,
    # neutral name since both consume it), so the two cannot drift.
    note_excerpt_chars: int = Field(default=240, gt=0)
    # Cap on how many evidence chunks `gather_evidence` hands the agent in one sweep, so a
    # broad question over a large corpus fills only as much context as it needs (the agent
    # narrows the query or drills in with expand_note when the sweep is truncated).
    gather_evidence_max_chunks: int = Field(default=40, ge=1)
    # Rank-before-truncate for the evidence sweep (KM-5): when `gather_evidence` exceeds its cap it
    # keeps the highest-scored chunks, not an arbitrary disk-order slice. Graph hits score by note
    # `confidence` (this default when a note has none), structural hits by their similarity — so a
    # broad sweep drops the least-supported evidence first, not whatever parsed last.
    retrieval_default_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    # Cache the parsed knowledge graph so interactive retrieval does not re-read + re-parse the
    # whole `knowledge_dir` on every query (KM-14). The cache is keyed by a cheap stat fingerprint
    # of the note tree (path + mtime + size), so any add/edit/delete of a note busts it — retrieval
    # stays always-live. Off makes every call re-parse (the pre-cache behavior); leave on in prod.
    graph_cache_enabled: bool = True


class ReportSettings(BaseSettings):
    """The report harness (plan Phase 5b) and sub-agent fan-out (F10-D).

    Grouped because both knobs govern durable fan-out work: a report's
    per-section activity budget and the concurrency bound on child workflows
    (report sections, memory-synthesis groups).
    """

    # Per-section retrieval budget for the durable development-report workflow — one section is
    # one activity, so a long report resumes section by section after a worker restart.
    report_section_timeout_seconds: float = Field(default=300.0, gt=0)
    # A fan-out job (report sections, memory-synthesis groups) runs its independent sub-tasks as
    # child workflows; this bounds how many run at once so a large report/corpus does not spawn
    # hundreds of children simultaneously. Per-child retry + durability come from each child's
    # own retry policy; the bound is on concurrency only.
    orchestrator_max_parallel_children: int = Field(default=8, ge=1)


class Settings(
    ObservabilitySettings,
    TemporalSettings,
    StoreSettings,
    HpcSettings,
    CalculatorSettings,
    BoSettings,
    LlmSettings,
    AgentSettings,
    ServiceSettings,
    EntraSettings,
    KgSettings,
    EvalSettings,
    FingerprintSettings,
    ElnSettings,
    SourcesSettings,
    MemorySettings,
    RetrievalSettings,
    ReportSettings,
):
    """Environment configuration, loaded from process env then a local `.env`.

    Field names map to `CHEMCLAW_<FIELD>` environment variables (e.g.
    `CHEMCLAW_TEMPORAL_ADDRESS`). Defaults target the local `docker-compose`
    dev stack so a fresh checkout runs without any `.env` present.

    Composed from the per-domain section mixins above; every field stays a flat
    attribute with its original env name, and this `model_config` (prefix,
    `.env`, `extra="forbid"`) governs them all.
    """

    model_config = SettingsConfigDict(
        env_prefix="CHEMCLAW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )


settings = Settings()
"""Process-wide configuration singleton. Import this, not the class."""
