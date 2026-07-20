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

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Temporal — durable execution of long scientific jobs (plan Phase 1).
    # `address` is the frontend gRPC endpoint; `namespace` isolates a team's jobs.
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"

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

    # MAF agent (plan step 1.5). `agent_model` is the orchestration model name
    # (ENV-overridable); the provider's API key is read by the chat client from
    # its own env var (e.g. ANTHROPIC_API_KEY), not stored here. `skills_dir` is
    # where the agent discovers SKILL.md files.
    agent_model: str = "claude-sonnet-5"
    skills_dir: str = "skills"

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
    eval_ab_epsilon: float = 0.0

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
    # How much of a source note's body a report carries as an evidence excerpt.
    report_excerpt_chars: int = Field(default=240, gt=0)

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
