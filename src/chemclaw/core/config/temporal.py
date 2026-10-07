"""Settings for Temporal: durable execution of long scientific jobs.

One domain section of the composed `Settings`; the package `__init__.py` flattens the sections and
owns the env prefix, `.env` loading and cross-section validators.
"""

from typing import Self

from pydantic import Field, SecretStr, model_validator
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
    # Transport security. Identity rides in the workflow payload (`requested_by`), so the transport
    # is authenticated with mTLS (PEM paths for cert, key and server-root CA) or an API key. All
    # empty for the local dev broker.
    temporal_tls_cert: str = ""
    temporal_tls_key: str = ""
    temporal_tls_ca: str = ""
    # A `SecretStr`, so the value cannot reach logs, dumps or validation errors; read it with
    # `.get_secret_value()`.
    temporal_api_key: SecretStr = SecretStr("")

    # Core's own task queue (sync, re-index, reports, the connector-job wrapper). Each bundle's
    # durable work runs on its own derived queue (`connectors.queues.bundle_queue`).
    background_task_queue: str = "background-jobs"

    # Start-to-close for a short core activity that computes or writes and returns
    # (`durable/memory_jobs.py`, `durable/orchestrator.py`, `durable/notify.py`). Longer activities
    # state their own budget.
    activity_timeout_seconds: float = Field(default=30.0, gt=0)

    # `schedule_to_start_timeout` on every core activity (`durable/publish.py::queue_wait_timeout`):
    # `start_to_close` does not start until a worker picks the task up, so an unserved queue waits
    # forever. A ScheduleToStart timeout is not retried, so this turns an infinite wait into one
    # loud failure while retries keep their meaning. An hour, far above normal backpressure.
    activity_queue_wait_seconds: float = Field(default=3600.0, gt=0)

    # Ceiling on one scheduled run, as `run_timeout` (`durable/schedules.py`); `execution_timeout`
    # would bound the whole `continue_as_new` chain. Schedules use `SKIP` overlap, so a run that
    # never ends would skip every later fire silently. `ScheduleHealth.last_outcome` reports
    # `TIMED_OUT` for a killed run. `corpus_sync` and `document_sync` keep no cursor between runs,
    # so a killed run restarts from page one.
    schedule_run_timeout_seconds: float = Field(default=86400.0, gt=0)

    # Retry bound under `workflows.publish.BAD_DATA_RETRY`: bad data is non-retryable by type; this
    # caps transient retries so an unclassified deterministic failure gives up.
    activity_max_attempts: int = Field(default=5, ge=1)

    # Retries of a template's agent step (`publish.agent_step_retry`); 1 = none. A retry replays the
    # whole turn and re-runs its side-effecting tools, while the provider SDK already retries a 503
    # in-process (`llm_max_retries`). A long outage therefore fails the step; raise this only
    # knowing what each attempt may duplicate.
    agent_step_max_attempts: int = Field(default=1, ge=1)

    # Concurrent activities per worker process. Equal to the Postgres pool width, so no activity
    # waits for a connection (temporalio's default of 100 turns starvation into retry churn).
    # Bundles whose activities wait rather than query (`calc`) override it in the chart.
    worker_max_concurrent_activities: int = Field(default=8, ge=1)

    # Workflows a worker keeps resident between tasks (SDK default 1,000). Task slots only bound
    # workflows being advanced; this cache is what holds memory. Per-workflow cost is a fixed
    # overhead, ~1x its state and a term growing with history. 750 keeps the shipped worker memory
    # request clear with margin for the uncertain history term; `tests/test_workers.py` holds the
    # inequality. An evicted workflow is replayed from history on its next task: CPU and broker
    # traffic, never a wrong answer.
    worker_max_cached_workflows: int = Field(default=750, ge=1)

    # Ceiling `durable/interceptor.py` holds every serialized activity result to. The broker's blob
    # limit (2 MiB default) fails the workflow after the activity logged success, and the gRPC frame
    # limit (4 MiB) makes the worker retry forever as a network error; refusing here is counted,
    # logged and attributed. Match it to the server's `limit.blobSize.error`.
    activity_result_max_bytes: int = Field(default=2 * 1024 * 1024, gt=0)

    # Heartbeat timeout for core's long background activities (`durable/note_index.py`,
    # `durable/retention.py`, `durable/publish_results.py`), so a dead worker is noticed in a
    # minute, not at start-to-close. `durable/heartbeat.py::beating` derives the beat from it.
    background_activity_heartbeat_timeout_seconds: float = Field(default=60.0, gt=0)

    # How often a worker re-reads the count of open durable jobs (`durable/job_metrics.py`); a
    # scrape must not make a network call, so the gauge is at most this old.
    jobs_in_flight_refresh_seconds: float = Field(default=30.0, gt=0)

    # Ceiling on a durable wait's `deadline_days`: a wait holds a workflow open on the broker.
    awaiting_max_days: float = Field(default=90.0, gt=0)
    # Budget for a wait's projection writes and push-back; separate from `activity_timeout_seconds`
    # so tightening that cannot break a wait's bookkeeping.
    awaiting_activity_timeout_seconds: float = Field(default=30.0, gt=0)
    # Cadence of the sweep that settles waits whose run can no longer settle its own row
    # (`durable/orphaned_waits.py`).
    awaiting_orphan_sweep_minutes: float = Field(default=60.0, gt=0)
    # Minimum age before the sweep asks about a row's run; younger rows are likely still opening.
    # The settle also guards on `run_id`.
    awaiting_orphan_grace_seconds: float = Field(default=300.0, ge=0)
    # Rows per keyset page of the orphan sweep (one `describe` each). A pass stops at half of
    # `retention_timeout_seconds` (`orphaned_waits._PASS_BUDGET_FRACTION`).
    awaiting_orphan_batch: int = Field(default=200, gt=0)
    # Check-ins tell a requester about their own blocked work (`durable/check_in.py`), which they
    # would otherwise hear about only on expiry. Delivered to `GET /check-ins` and outbound
    # channels; each night supersedes the unread notice, so one row per requester. `Chemclaw3_ui`
    # does not render them yet.
    check_in_enabled: bool = True
    # Days a question must have been open before a check-in mentions it.
    check_in_quiet_days: float = Field(default=3.0, gt=0)
    check_in_schedule_minutes: float = Field(default=1440.0, gt=0)
    check_in_timeout_seconds: float = Field(default=60.0, gt=0)

    # The calculation backend's admission budget. The per-process cap
    # (`worker_max_concurrent_activities`) times worker replicas is what `servers/calc` sees; beyond
    # its capacity the pod thrashes, trips heartbeats and gets retried onto itself. Declared here,
    # not in `calculators.py`, because that section's names are also read by the calc server.
    #
    # `calc_fleet_worker_processes` comes from the chart's `calc` `workerReplicas`; 0 means no
    # durable calc worker. `calc_backend_max_concurrent_requests` is the server's admission
    # capacity; startup refuses a product above it, and 0 disables the check. Tool-call traffic is
    # seen only at runtime (`chemclaw_calc_requests_in_flight`).
    calc_fleet_worker_processes: int = Field(default=1, ge=0)
    calc_backend_max_concurrent_requests: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _temporal_mtls_is_complete(self) -> Self:
        """A Temporal client cert without its key (or vice versa) is a silent half-config.

        A server-root CA alone (server auth only) is fine.
        """
        if bool(self.temporal_tls_cert) != bool(self.temporal_tls_key):
            raise ValueError("temporal_tls_cert and temporal_tls_key must be set together")
        return self
