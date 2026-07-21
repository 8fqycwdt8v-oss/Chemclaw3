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
"""

import os
import sys
from typing import Literal

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


class Settings(BaseSettings):
    """Environment configuration, loaded from process env then a local `.env`.

    Field names map to `CHEMCLAW_<FIELD>` environment variables (e.g.
    `CHEMCLAW_TEMPORAL_ADDRESS`). Defaults target the local `docker-compose`
    dev stack so a fresh checkout runs without any `.env` present.
    """

    model_config = SettingsConfigDict(
        env_prefix="CHEMCLAW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    # Logging / observability. One config-driven switch for verbosity so an admin can raise
    # it to DEBUG for troubleshooting without touching code; the format carries the timestamp,
    # level, and logger name every diagnosis needs. Applied once per process by
    # `chemclaw.logging.configure_logging`, called at each worker's entrypoint.
    log_level: str = "INFO"
    log_format: str = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    # GxP tool-audit trail (agents.audit): every agent tool call is logged once (name, args,
    # outcome, latency) by one MAF function middleware. Arguments are truncated to this many
    # characters so a large payload (a full optimization problem, an observation list) cannot
    # flood the log; raise it when a fuller argument record is needed for an audit.
    agent_audit_max_arg_chars: int = Field(default=200, ge=0)
    # OpenTelemetry export (off by default). When enabled, `chemclaw.logging.configure_telemetry`
    # calls MAF's `configure_otel_providers`, which reads the standard `OTEL_EXPORTER_OTLP_*`
    # environment variables for the collector endpoint. Requires the OpenTelemetry SDK + OTLP
    # exporter extras to be installed; `enable_sensitive_data` controls whether prompts/results
    # are attached to spans (keep off unless a trusted collector needs them).
    otel_enabled: bool = False
    otel_include_sensitive_data: bool = False

    # Temporal — durable execution of long scientific jobs (plan Phase 1).
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

    # Postgres/pgvector — fingerprint store (Phase 3) and QM result cache
    # (plan step 1.10). One DSN for the whole app.
    postgres_dsn: str = "postgresql://chemclaw:chemclaw@localhost:5432/chemclaw"
    # Fail fast when the database is unreachable instead of hanging until the
    # enclosing activity's start-to-close timeout expires (libpq connect_timeout).
    pg_connect_timeout_seconds: int = Field(default=10, gt=0)
    # Per-statement wall-clock bound for the store connections (libpq
    # statement_timeout). A hung query is cancelled after this instead of consuming
    # the whole enclosing activity's start-to-close budget. 0 disables it; migrations
    # deliberately connect without a statement timeout (an index build may be slow).
    pg_statement_timeout_seconds: float = Field(default=30.0, ge=0)

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
    # The HPC/Nextflow identity bridge (plan F4-T6, §7.2): the other non-Entra bridge. HPC is not an
    # Entra relying party, so user jobs run under one service identity while the requesting Entra
    # `oid` is carried in the payload (F4-T3) and *every* oid→HPC-identity mapping is logged for the
    # audit trail. This is the shared service identity jobs run as.
    hpc_bridge_identity: str = "chemclaw-hpc"

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

    # Durable BO campaign (plan step 1d.4). A single round (BoFire propose + evaluate)
    # can be slow, so activities get a generous start-to-close budget.
    bo_activity_timeout_seconds: float = Field(default=300.0, gt=0)
    # Seed for BoFire's random design + SOBO strategies, so a campaign is reproducible
    # (deterministic seeding + proposals) rather than flaky run-to-run.
    bo_seed: int = 42

    # LLM provider seam (plan Phase F0). The agent's chat client is selected by config, so the
    # deployment can point the agent at the internal OpenAI-compatible ("OpenLLM-like") endpoint
    # without any code change, keeping Anthropic as a local-dev path. `openai_compatible` reaches
    # the endpoint with **one generic API credential** (`llm_api_key`) — deliberately *not* per-user
    # Entra: the raw inference call is not a user-scoped resource (see docs/foundation-plan.md §0).
    # `llm_base_url`/`llm_model` are required for `openai_compatible` (validated below); the TLS CA
    # bundle, timeout, and retry budget shape the transport so an internal endpoint with a private
    # CA works from config alone. `llm_temperature`/`llm_max_tokens` are the default generation
    # params threaded into the agent (F0.3). The default provider is `anthropic` so a fresh checkout
    # config singleton is valid with no endpoint set; production sets `CHEMCLAW_LLM_PROVIDER=
    # openai_compatible` + the base_url/model. No provider client class is imported outside
    # `agents/llm_provider.py`.
    llm_provider: Literal["openai_compatible", "anthropic"] = "anthropic"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_tls_ca_bundle: str = ""
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=3, ge=0)
    llm_temperature: float = Field(default=0.0, ge=0)
    llm_max_tokens: int = Field(default=4096, gt=0)

    # MAF agent (plan step 1.5). `agent_model` is the orchestration model name
    # (ENV-overridable); the provider's API key is read by the chat client from
    # its own env var (e.g. ANTHROPIC_API_KEY), not stored here. `skills_dir` is
    # where the agent discovers SKILL.md files — one or more directories, delimited by the
    # OS path separator (like PATH), so an admin can add a second (e.g. team-private) skills
    # directory without code changes. Read it through the `skills_dirs` property, never raw.
    agent_model: str = "claude-sonnet-5"
    skills_dir: str = "skills"
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

    # Front-door run service (plan Phase F2) — the ASGI service that actually *runs* the agent for a
    # chemist: it builds the agent, opens the MCP tool lifecycle for the turn, streams the response,
    # and serves the browser chat surface. `service_host`/`service_port` bind the server (the
    # OpenShift Route front-ends it, F6). `service_cors_origins` is a comma-separated allow-list for
    # browser origins that may call the API (empty = none, the safe default; a same-origin embedded
    # UI needs none). These are the only front-door knobs; identity/OIDC is layered on in F4.
    # Binds all interfaces inside the container; the OpenShift Route + NetworkPolicy gate ingress.
    service_host: str = "0.0.0.0"
    service_port: int = Field(default=8080, gt=0)
    service_cors_origins: str = ""

    # Durable session store (plan Phase F3). The agent's conversation history must survive a pod
    # restart, so a session is resumable. `memory` keeps the classic in-process provider (dev/test);
    # `postgres` persists each turn's messages to `session_messages` keyed by session id, so a fresh
    # process over the same DSN resumes the thread. **Session state is not Temporal job state** — it
    # is the conversation layer (D-002), and compaction still runs on top. `session_store_dsn` lets
    # the session store point at a different database than the calculation/fingerprint DSN; empty
    # falls back to `postgres_dsn` (one database in the simple deployment).
    session_store: Literal["memory", "postgres"] = "memory"
    session_store_dsn: str = ""
    # Job→session push-back (plan F3-T2/T3): a finished Temporal job writes a `session_events` row;
    # the front door tails the table and wakes the owning session (appending the result, flipping
    # the `awaiting` todo) instead of the user polling. This is the tailer's poll interval — a
    # LISTEN/NOTIFY-free fallback that is simple and correct; lower it for snappier wake-ups.
    session_event_poll_seconds: float = Field(default=2.0, gt=0)

    # Azure Entra ID identity (plan Phase F4). User auth at the front door is OIDC with Entra as the
    # IdP: the service is an Entra app registration, and every non-health request carries an Entra
    # JWT that is validated against the tenant JWKS with the audience checked (the confused-deputy
    # guard — the service is both OAuth client and resource). `oid`/`upn` + app-roles are extracted
    # into a `Principal` that authorizes and attributes every backend action. `entra_required` gates
    # enforcement: True in any real deployment (a missing/invalid token is 401); False only for
    # local dev, where a stand-in principal runs the app without a tenant. `entra_jwks_url`/
    # `entra_issuer` default empty and derive from `entra_tenant_id` when set (the standard v2.0
    # endpoints), so a deployment sets just tenant + client (audience) + required.
    entra_required: bool = False
    entra_tenant_id: str = ""
    entra_client_id: str = ""
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

    # Markdown knowledge graph (plan Phase 2). Directory of note files the indexer
    # reads; retrieval is graph traversal over their [[wikilinks]] (D-004).
    knowledge_dir: str = "knowledge"
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

    # Bound on retries for ordinary activities under the shared bad-data retry policy
    # (`workflows.publish.BAD_DATA_RETRY`). Bad data is non-retryable by type; this caps
    # the *transient* retries so an unclassified deterministic failure (a bug, not a
    # network blip) gives up instead of pinning a worker with unlimited retries.
    activity_max_attempts: int = Field(default=5, ge=1)

    # How long a confirmed-answer note is held pending a human Yes/No before the
    # hold expires unpublished (plan step 5.5, async approval seam). The button click
    # is a Temporal signal into `InteractionApprovalWorkflow`; this bounds the wait so
    # an unanswered prompt cannot pin a workflow forever. Default 7 days — generous for
    # an out-of-band review, still finite.
    interaction_approval_timeout_seconds: float = Field(default=604800.0, gt=0)

    # Evaluation & metric layer (plan Phase 2b). A metric is a pure function; its
    # pass/fail threshold is config, never hardcoded (G3). The green-chemistry
    # limits are dimensionless (kg waste or input per kg product) and process-
    # dependent — these defaults are lenient gate values, tune them per chemistry.
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

    # Fingerprint search (plan Phase 3, mcp-molfp). ECFP4 = Morgan radius 2, 2048 bits;
    # both are config so the fingerprint definition (and thus the stored column width)
    # is a deliberate, versioned choice, not a magic number. The similarity threshold is
    # the Tanimoto floor a match must clear to count as a structural neighbor — the
    # capability exposes it, the `reaction-search` skill decides how to wield it (G6).
    ecfp_radius: int = Field(default=2, ge=0)
    ecfp_bits: int = Field(default=2048, gt=0)
    # DRFP reaction fingerprint width (plan step 3.4, mcp-rxnfp). Its own field, not shared
    # with ecfp_bits — a different fingerprint whose folded length is an independent choice,
    # though both default to 2048 (matching their bit(N) columns). top_k/threshold below are
    # shared: they are generic fingerprint-search knobs, not molecule-specific.
    drfp_bits: int = Field(default=2048, gt=0)
    fingerprint_top_k: int = Field(default=10, ge=1)
    fingerprint_similarity_threshold: float = Field(default=0.3, ge=0.0, le=1.0)

    # ELN ingestion (plan Phase 4). The one concrete adapter reads a JSON-export ELN from
    # this directory; the sync activity's timeout bounds one batch of fetch+validate+index+
    # PR-gate work. ELN-specific format lives only in the adapter, never in config (G6).
    eln_export_dir: str = "eln/exports"
    eln_sync_timeout_seconds: float = Field(default=300.0, gt=0)
    # A second concrete adapter reads native Open Reaction Database messages (human-readable
    # ORD JSON) from this directory — the "structured recipe" path, alongside the free-text
    # JSON export above. Same `ElnAdapter` contract, so both flow through the one sync loop.
    ord_export_dir: str = "eln/exports/ord"
    # Which registered ELN adapter the durable sync ingests from (a key of `eln.registry`'s
    # `ELN_ADAPTERS`: "json" for the free-text export, "ord" for native ORD). The sync tracks
    # one high-water cursor, so it runs a single source; switching source is this setting, not
    # a code change. (The memory jobs read the union of all registered adapters instead.)
    eln_sync_adapter: str = "json"

    # Temporal Schedules that drive the periodic background jobs (`scripts/schedules.py`,
    # applied by `make schedules-apply`). Intervals in minutes: how often each workflow fires
    # on the background queue. Schedules live in Temporal (durability there, not host cron).
    # The ELN sync is self-cursoring (loads/stores its high-water mark in `sync_cursors`), so
    # its Schedule passes no argument; the memory-synthesis jobs re-scan the whole corpus, so
    # they run less often. Overridable so a deployment tunes cadence without code change.
    eln_sync_schedule_minutes: float = Field(default=60.0, gt=0)
    memory_synthesis_schedule_minutes: float = Field(default=1440.0, gt=0)

    # Memory layers (plan Phase 5). The semantic layer distils a playbook only from reactions
    # whose DRFP similarity clears this floor and that recur across >=2 projects — higher than
    # the search floor, since a playbook claims "same transformation", not just "related".
    playbook_similarity_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    # The episodic layer groups an *optimization campaign* — repeated runs of the **same
    # transformation** (a screen varying conditions/reagents) — by DRFP similarity. Higher than
    # the playbook floor: an optimization series is the same reaction re-run, not merely related
    # chemistry, so the grouping must be tight to avoid merging distinct transformations.
    optimization_similarity_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    memory_job_timeout_seconds: float = Field(default=300.0, gt=0)

    # Report harness (plan Phase 5b). Per-section retrieval budget for the durable
    # development-report workflow — one section is one activity, so a long report resumes
    # section by section after a worker restart.
    report_section_timeout_seconds: float = Field(default=300.0, gt=0)
    # How much of a source note's body an excerpt carries — shared by the report harness's
    # evidence excerpts and the memory layer's procedure excerpts (one note-excerpt budget,
    # neutral name since both consume it), so the two cannot drift.
    note_excerpt_chars: int = Field(default=240, gt=0)
    # Cap on how many evidence chunks `gather_evidence` hands the agent in one sweep, so a
    # broad question over a large corpus fills only as much context as it needs (the agent
    # narrows the query or drills in with expand_note when the sweep is truncated).
    gather_evidence_max_chunks: int = Field(default=40, ge=1)

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

    @property
    def skills_dirs(self) -> list[str]:
        """The skills directories, split on the OS path separator (like PATH), empties dropped.

        `FileSkillsSource` takes a list of directories; keeping the config a single delimited
        string (rather than a JSON list) means an admin sets `CHEMCLAW_SKILLS_DIR=skills:/opt/
        team-skills` the same way they set `PATH`, no JSON quoting.
        """
        return [d for d in self.skills_dir.split(os.pathsep) if d]

    @model_validator(mode="after")
    def _knowledge_dir_is_relative(self) -> "Settings":
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

    @model_validator(mode="after")
    def _llm_provider_config(self) -> "Settings":
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
    def _poll_faster_than_heartbeat(self) -> "Settings":
        """The poll loop must beat faster than Temporal's heartbeat timeout.

        Otherwise every `poll_hpc_status` activity is declared dead between two
        heartbeats and retried in a loop — a mis-set interval must fail at startup.
        """
        if self.hpc_poll_interval_seconds >= self.qm_poll_heartbeat_timeout_seconds:
            raise ValueError(
                "hpc_poll_interval_seconds must be smaller than qm_poll_heartbeat_timeout_seconds"
            )
        return self


settings = Settings()
"""Process-wide configuration singleton. Import this, not the class."""
