"""Temporal — durable execution of long scientific jobs (plan Phase 1).

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class TemporalSettings(BaseSettings):
    """Temporal — durable execution of long scientific jobs (plan Phase 1).

    Grouped because everything here shapes how the app reaches and uses the one Temporal cluster:
    the frontend endpoint, transport security, the two task queues from the architecture, and
    the shared activity retry bound.
    """

    # `address` is the frontend gRPC endpoint; `namespace` isolates a team's jobs.
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    # Securing the Temporal transport (plan F4-T6, §7.2): one of the two non-Entra bridges.
    # Identity rides *inside* the workflow payload (`requested_by`, F4-T3), never the transport
    # — so the transport is authenticated with mTLS (client cert/key + server-root CA, paths to
    # PEM files) or a Temporal Cloud API key, not with a user token. All empty in local dev (a
    # plaintext dev broker); a deployment sets the mTLS trio or the API key.
    temporal_tls_cert: str = ""
    temporal_tls_key: str = ""
    temporal_tls_ca: str = ""
    temporal_api_key: str = ""

    # Core's own task queue: the light background jobs (sync, re-index, reports, and the
    # connector-job wrapper). A name is config so a deployment can shard or rename it without
    # touching worker code (D-006). There is no second core queue any more — the heavy `hpc-jobs`
    # queue went with the QM job into `connectors/qm/`, whose worker derives its queue from the
    # bundle name (`connectors.queues.bundle_queue`, D-118).
    background_task_queue: str = "background-jobs"

    # Bound on retries for ordinary activities under the shared bad-data retry policy
    # (`workflows.publish.BAD_DATA_RETRY`). Bad data is non-retryable by type; this caps the
    # *transient* retries so an unclassified deterministic failure (a bug, not a network blip)
    # gives up instead of pinning a worker with unlimited retries.
    activity_max_attempts: int = Field(default=5, ge=1)

    # Bound on retries for a template's **agent** step alone (`durable/template_job.py`,
    # `publish.agent_step_retry`). 1 = no outer retry.
    #
    # Its own setting because an agent step is the one activity whose retry is not free.
    # Measured: a single provider 503 produced **two PR-gate branches and two audit rows for one
    # logical note**, because a Temporal retry replays the whole turn from the prompt — there is no
    # checkpointer behind an activity — so every tool the failed attempt already ran runs again,
    # side effects and all. The turn is not idempotent, and `activity_max_attempts` was silently
    # assuming it was.
    #
    # The retry that actually helps is already there and is much cheaper: the provider SDK retries
    # a 503 in-process, `llm_max_retries=3` giving 4 HTTP attempts (the SDK's base client loops
    # `range(max_retries + 1)` — measured, not assumed), with none of the replay. Wrapping that in
    # `activity_max_attempts=5` meant up to 20 HTTP attempts and up to 5 duplicated turns for one
    # blip.
    #
    # **The accepted cost, stated rather than discovered:** a *long* provider outage now fails the
    # step after ~4 HTTP attempts instead of riding it out over 20. That is the deliberate trade —
    # a template run that fails cleanly and is re-run by a person costs less than duplicate notes
    # and duplicate audit rows that a person has to find and reconcile. A deployment that would
    # rather ride out an outage raises this, knowing what each extra attempt may duplicate.
    agent_step_max_attempts: int = Field(default=1, ge=1)

    # How many activities one worker process may run at once
    # (D-2026-08-05-a-worker-may-not-outrun-its-pool).
    #
    # Set because temporalio's default is **100**, and it was reaching a Postgres pool of 8 — a
    # worker that may run twelve times more activities than it can borrow connections. The
    # shortfall is not a crash: `db.connection` raises `ConnectionError` after
    # `pg_pool_timeout_seconds`, Temporal classes that as transient and retries the activity, and
    # the work eventually gets done. But it gets done as retry churn rather than as backpressure —
    # each starved activity burns one of `activity_max_attempts` before it has computed anything,
    # and the honest reading of the state (`chemclaw_pg_pool_requests_waiting`) was not exported by
    # a worker at all until the same review.
    #
    # 8 rather than the pool's size, and deliberately equal to it rather than below: an activity
    # borrows a connection for a fraction of its runtime, so a bound *at* the pool width already
    # leaves the pool mostly idle, and going under it would cap throughput on a resource that is
    # not the constraint. Equal is the point at which no activity can ever be the one that has to
    # wait.
    #
    # A bundle whose activities are long waits rather than database work overrides this — `qm`
    # holds one slot per in-flight HPC job for up to `hpc_run_timeout_seconds` (24 h) and touches
    # the database twice in that span, so its ceiling is about memory, not connections, and its
    # chart entry says so.
    worker_max_concurrent_activities: int = Field(default=8, ge=1)

    @model_validator(mode="after")
    def _temporal_mtls_is_complete(self) -> Self:
        """A Temporal client cert without its key (or vice versa) is a silent half-config.

        mTLS needs both the client cert and its private key; a server-root CA alone (server-auth
        only) is fine. Rejecting cert-xor-key at startup beats a confusing handshake failure
        later.
        """
        if bool(self.temporal_tls_cert) != bool(self.temporal_tls_key):
            raise ValueError("temporal_tls_cert and temporal_tls_key must be set together")
        return self
