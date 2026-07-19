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

    # Deployment context. Kept free-form (dev/ci/staging/prod) rather than an
    # enum so ops can name environments without a code change.
    app_env: str = "dev"

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


settings = Settings()
"""Process-wide configuration singleton. Import this, not the class."""
