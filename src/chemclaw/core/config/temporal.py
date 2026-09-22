"""Temporal — durable execution of long scientific jobs (plan Phase 1).

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
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
    # Securing the Temporal transport (plan F4-T6, §7.2): one of the two non-Entra bridges.
    # Identity rides *inside* the workflow payload (`requested_by`, F4-T3), never the transport
    # — so the transport is authenticated with mTLS (client cert/key + server-root CA, paths to
    # PEM files) or a Temporal Cloud API key, not with a user token. All empty in local dev (a
    # plaintext dev broker); a deployment sets the mTLS trio or the API key.
    temporal_tls_cert: str = ""
    temporal_tls_key: str = ""
    temporal_tls_ca: str = ""
    # A `SecretStr`, like every other credential on this object
    # (`D-2026-08-26-a-credential-is-a-type-not-a-convention`): its `repr` is `**********`, so the
    # value cannot reach a log line, a `model_dump()` or a pydantic error message through a route
    # `core/logging.py`'s exact-match redaction has not been taught about. That filter stays and is
    # still the control; this is the type making the same guarantee where the filter is not looking.
    # Read it with `.get_secret_value()` — and note that an f-string does *not*, so a formatted
    # credential renders as asterisks and fails as a 401 rather than leaking.
    temporal_api_key: SecretStr = SecretStr("")

    # Core's own task queue: the light background jobs (sync, re-index, reports, and the
    # connector-job wrapper). A name is config so a deployment can shard or rename it without
    # touching worker code (D-006). It is the only core queue: every capability's durable work
    # runs on the queue its own bundle derives from its name (`connectors.queues.bundle_queue`,
    # D-118).
    background_task_queue: str = "background-jobs"

    # Start-to-close budget for a *short* core activity — one that computes or writes a row and
    # returns, rather than waiting on something. Read by the memory-distillation job, the
    # orchestrator's step activities and the session push-back
    # (`durable/memory_jobs.py`, `durable/orchestrator.py`, `durable/notify.py`).
    #
    # It used to be spelled `qm_activity_timeout_seconds` and live in an HPC section, which was
    # never what it meant: none of its three readers is a QM path, and the one bundle whose name it
    # carried is gone (`D-2026-08-26-semiempirical-is-the-whole-tier`). A capability whose
    # activities are longer than this states its own budget in its own workflow, as `calc` does.
    activity_timeout_seconds: float = Field(default=30.0, gt=0)

    # How long an activity task may sit on a queue *before a worker picks it up*, as
    # `schedule_to_start_timeout` on every core activity (`durable/publish.py::queue_wait_timeout`).
    #
    # `start_to_close_timeout` does not bound this and reading it as if it did is what left every
    # durable job unbounded: it starts counting at the *first attempt*, so a task nobody polls — the
    # background fleet scaled to zero, a rolling update, a queue named in config but served by no
    # pod — waits forever. `durable/notify.py` measured that shape (a workflow still RUNNING after
    # 75 s against a 30 s start-to-close) for one call; it is a property of the timeout, not of that
    # call.
    #
    # **Measured against the test server, because the retry interaction decides whether this is
    # safe:** a ScheduleToStart timeout is *not* retried. An activity with `maximum_attempts=3` and
    # a 10 s bound against an unserved queue failed once, at 10.028 s — so this bound converts an
    # infinite wait into one loud failure and leaves `start_to_close` and the retry policy to mean
    # exactly what they meant before. That is why it is this timeout rather than
    # `schedule_to_close_timeout`, which would have capped every attempt *together* and silently
    # deleted the retries at 31 call sites.
    #
    # An hour, deliberately far above any healthy queue delay: a wait on `background-jobs` is
    # ordinary backpressure — eight concurrent slots, activities that hold one for up to a quarter
    # of an hour — and turning backpressure into a non-retryable failure would be worse than the
    # wedge this exists to end. What an hour cannot be is normal, so a run that hits it is a fleet
    # fault and now says so.
    activity_queue_wait_seconds: float = Field(default=3600.0, gt=0)

    # Ceiling on one *scheduled* run (`durable/schedules.py`, as `run_timeout` — see there for why
    # it cannot be `execution_timeout`, which would bound the whole `continue_as_new` chain).
    #
    # Every Schedule here is `ScheduleOverlapPolicy.SKIP`, which is right — a re-scan that overruns
    # its interval must finish rather than queue a redundant twin — and is exactly why a run that
    # never finishes is worse than a failed one: it skips every subsequent fire of that job family,
    # indefinitely, and a skipped fire is not an error anywhere. The ceiling ends that wedge.
    #
    # **It does not make the kill visible on its own**, and for a while nothing here did.
    # `ScheduleInfo` supplies no outcome — `recent_actions` names the workflow and when it started,
    # and there is no failure counter — so with the ceiling, a schedule whose every run is killed
    # reported `runs_total` climbing, `last_run` advancing, `running_now` 0 and `skipped_overlap` 0,
    # byte-identical to a healthy job, while the wedge it replaces had a distinctive signature on
    # that surface (`last_run` frozen, `running_now` stuck at 1, `skipped_overlap` climbing). The
    # kill was therefore visible in the Temporal UI's per-workflow status and nowhere else here.
    # `ScheduleHealth.last_outcome` closes that: `durable/schedules.py::_last_outcome` describes the
    # newest recent action that is not still running — one extra bounded lookup per schedule — and
    # reports Temporal's own `TIMED_OUT` for a run this ceiling killed.
    #
    # A day, because none of these jobs legitimately runs for one, and what a terminated run costs
    # differs per job rather than being uniformly small: the ELN sync is cursored in `sync_cursors`
    # and loses at most the chunk in flight; retention, the reindex and the digest are idempotent or
    # advance a watermark; `label_sync` persists its progress in the stale set. `corpus_sync` and
    # `document_sync` keep **no** row between runs and say so in their own module docstrings, so a
    # terminated run of either restarts from its first page — cheap for the stat-only document
    # crawl, a repeat of the whole drain for a corpus load. That is what makes bounding *one run*
    # rather than the chain load-bearing rather than a detail.
    schedule_run_timeout_seconds: float = Field(default=86400.0, gt=0)

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
    # A bundle whose activities are long waits rather than database work overrides this — `calc`
    # holds a slot for the whole of a CREST conformer search and touches the database only at its
    # ends, so its ceiling is about memory, not connections, and its chart entry says so.
    worker_max_concurrent_activities: int = Field(default=8, ge=1)

    # **What a worker holds between tasks, which no setting here chose until 2026-09-22.**
    # `max_concurrent_activities` bounds activities and nothing bounded the workflow side, so the
    # ceiling was whatever the SDK picks. Measured on this version (1.31.0): `Worker.__init__`
    # passes `None` through to `WorkerTuner.create_fixed`, whose `or 100` is the real default for
    # workflow-task slots — not the 500 the constructor's own docstring mentions, which belongs to
    # the *resource-based* tuner. And the workflow-task ceiling is not the one that holds memory:
    # a task slot is occupied only while a workflow is being advanced, while `max_cached_workflows`
    # (SDK default 1,000) is what keeps a started workflow resident between its tasks.
    #
    # **Measured against the real broker, not reasoned about.** A worker with N workflows parked in
    # `wait_condition`, RSS sampled from `/proc/self/status` against an idle baseline of 65.8 MiB:
    # 50 → 137 KiB each, 100 → 114, 250 → 82, 500 → 73, 1,000 → **71 KiB each**, converging as the
    # fixed cost amortises. Scaling the workflow's own state at 200 cached: 16 KiB of state → 91
    # KiB each, 64 → 142, 256 → 347. So a cached workflow costs about **75 KiB plus 1.35x whatever
    # it holds** — the excess being the event history the cache keeps for replay.
    #
    # (The first fixture said state was free, because `["y" * 1024 for _ in range(n)]` is
    # constant-folded into `n` references to **one** string. A live-instance count found it.)
    #
    # 1,000 at the shipped `resources.worker.requests.memory` of 1Gi is 75 MiB for trivial
    # workflows and ~340 MiB for ones carrying 256 KiB — a third of the request, before the worker
    # has done anything else. `tests/test_workers.py` holds the inequality against the chart rather
    # than restating a number here, which is the shape
    # `D-2026-09-18-a-second-process-in-the-pod-is-memory-the-chart-never-declared` uses.
    worker_max_cached_workflows: int = Field(default=1000, ge=1)

    # The ceiling `durable/interceptor.py` holds every activity *result* to, measured as the
    # serialized payload the worker is about to upload.
    #
    # **Two broker limits sit above this number and they have two different failure shapes**, which
    # is why the check is here rather than left to either of them. Driven against a live broker on
    # 2026-09-19:
    #
    # - Temporal's server-side **blob limit** (`limit.blobSize.error`, 2 MiB by default) refuses a
    #   single payload over it. A 3,000,000-byte result failed the workflow immediately — but our
    #   own `activity.finished` line had already said `completed`, because the upload happens after
    #   the interceptor returns.
    # - the SDK's **gRPC frame limit** (4 MiB after decompression) refuses the whole
    #   `RespondActivityTaskCompleted` message. A 6,000,000-byte result made the worker retry the
    #   attempt for ever against a `ResourceExhausted` it reports as a *network* error, and the
    #   workflow sat `RUNNING` until its own timeout — the shape an operator cannot diagnose,
    #   because no first-party series moves and no Python log line is written.
    #
    # 2 MiB, so the number this refuses at is the smaller of the two the broker enforces: a result
    # this check admits is one the shipped broker accepts, and a result it refuses is one that was
    # never going to arrive. A deployment that raises `limit.blobSize.error` (or installs a codec
    # that compresses payloads) raises this to match; one that lowers the server's limit lowers this
    # first, because a refusal *here* is counted, logged and attributed to a turn, and a refusal
    # there is a Rust WARN with no correlation id.
    activity_result_max_bytes: int = Field(default=2 * 1024 * 1024, gt=0)

    # The heartbeat timeout for core's own *long* background activities — the note reindex, the
    # retention sweep, the result-publication drain (`durable/note_index.py`,
    # `durable/retention.py`, `durable/publish_results.py`).
    #
    # `connectors/calc/workflows.py` states the rule these three were missing: "without a heartbeat
    # timeout those heartbeats do nothing for failure detection", so a worker that dies mid-activity
    # is noticed only when the *start-to-close* budget expires — 600 s for the reindex and the
    # sweep, and `result_publish_timeout_seconds x len(result_sink_list)` for the drain. On work
    # that normally finishes in seconds that is the difference between a retry and an idle
    # afternoon.
    #
    # One setting for all three rather than one each, because they are one kind of thing: a core
    # background activity with no internal unit boundary to report progress at, wrapped in
    # `durable/heartbeat.py::beating`, which derives its beat from exactly this number so the beat
    # and the timeout cannot drift. A capability whose activity is a different kind of thing states
    # its own, as `calc` does with `xtb_job_heartbeat_timeout_seconds`.
    #
    # 60 s: comfortably above the ~15 s beat it implies and far below every start-to-close budget it
    # sits under, so a dead worker is detected in a minute rather than in ten.
    background_activity_heartbeat_timeout_seconds: float = Field(default=60.0, gt=0)

    # How often a worker re-asks the broker how many durable jobs are open
    # (`durable/job_metrics.py`). A *reading* interval rather than a scrape-time query: a gauge
    # source is synchronous and a Prometheus scrape must not make a network call, so the number a
    # scrape sees is at most this old. 30 s because the thing being watched is a job that runs for
    # minutes to hours — a fresher reading would buy nothing and cost one visibility query per
    # worker per interval.
    jobs_in_flight_refresh_seconds: float = Field(default=30.0, gt=0)

    # The durable wait (D-2026-08-29-a-decision-that-waits-is-a-workflow).
    #
    # A ceiling rather than a default, and the two numbers answer different questions. A caller
    # states its own `deadline_days` — a plate turnaround is days, a gate review is weeks — and this
    # clamps it, because a wait is a workflow run held open on the broker and an unbounded one is a
    # resource nobody reclaims. Ninety days is longer than any deliberate ask this system makes and
    # far short of forever.
    awaiting_max_days: float = Field(default=90.0, gt=0)
    # The projection writes and the push-back are small row operations. Separate from
    # `activity_timeout_seconds` so tightening the general budget cannot silently make a wait's
    # bookkeeping the thing that fails, on a workflow whose entire purpose is to survive.
    awaiting_activity_timeout_seconds: float = Field(default=30.0, gt=0)
    # The check-in over a requester's own blocked work
    # (`D-2026-09-15-the-requester-hears-nothing-until-it-is-too-late`, `durable/check_in.py`).
    # The wait above already re-notifies `asked_of` on `reminder_hours`; the *requester* is
    # written to exactly once, on expiry — so with `awaiting_max_days` at 90 they can hear
    # nothing about their own suspended campaign for three months and then hear it failed.
    #
    # **On by default, and the reason it was off is worth keeping because half of it was real.**
    # It shipped off because the sweep delivers to a mailbox and an outbound channel, and a
    # deployment that had configured neither would be writing where nobody reads — the mistake
    # `D-2026-09-15-a-watch-that-nothing-evaluates-is-a-promise-a-deployment-cannot-keep` records.
    #
    # Two things changed. The mailbox now has a reader that ships: `GET /check-ins`
    # (`api/routes/streams.py`), whose absence was the original defect and is asserted end to end.
    # And the sweep no longer accumulates: it supersedes the unread notices of the page it is about
    # to write, so a requester holds **one** row rather than one per night — measured before that
    # fix at ~87 unprunable rows per requester over a 90-day wait, which is what made "writes
    # somewhere nobody reads" a storage problem as well as a pointless one.
    #
    # What has *not* changed, and is the honest residual: `Chemclaw3_ui` does not call
    # `GET /check-ins` yet (`docs/planning/BACKLOG.md` §5), so today a check-in reaches a chemist
    # through the API or an outbound channel and not through the app. That is a surfacing gap with
    # an owner, not a reason for the sweep to stay silent — the requester whose campaign is
    # suspended is worse served by nothing at all than by a notice their client has yet to render.
    check_in_enabled: bool = True
    # How long a question must have been open before it is worth mentioning. A question asked
    # this morning is not news to the person who asked it, and a check-in that said so on the
    # first night would train its reader to ignore the second.
    check_in_quiet_days: float = Field(default=3.0, gt=0)
    check_in_schedule_minutes: float = Field(default=1440.0, gt=0)
    check_in_timeout_seconds: float = Field(default=60.0, gt=0)

    # **The two halves of the calculation backend's admission budget**
    # (`D-2026-08-27-a-per-worker-cap-is-not-a-backend-ceiling`). Same shape as the fleet turn
    # ceiling and the Postgres connection budget one subject over, and for the same reason: the cap
    # that exists is per *process* — `worker_max_concurrent_activities` — while the thing being
    # protected is a single shared pod, so `replicas × that cap` is what `servers/calc` actually
    # sees and nothing computed it. Scaling the `calc` worker, an ordinary operational lever,
    # multiplied concurrent CPU-bound load on that pod invisibly; `OMP_NUM_THREADS=1` is pinned
    # there against intra-run contention, so the surplus arrives as thrashing, which trips
    # heartbeat timeouts, whose retries land back on the same overloaded pod.
    #
    # **Declared here rather than in `calculators.py`**, beside the per-process cap they multiply:
    # this is a budget for the *worker fleet*, and `tests/test_config.py` refuses a calculator
    # field whose only reader is a config validator — for the sharp reason that the calculation
    # server reads that section's names under the same env prefix.
    #
    # `calc_fleet_worker_processes` is how many worker processes may run `calc` activities at once
    # — the chart derives it from that bundle's `workerReplicas`, so it is the same number
    # Kubernetes obeys rather than a second copy of the topology. **0 is legal and means what it
    # says**: a release with no `calc` worker Deployment dispatches nothing durably, and rendering
    # a floor of 1 there would refuse a deployment over calculations it never makes.
    #
    # `calc_backend_max_concurrent_requests` is what that pod will serve, and it is a **provisioning
    # statement**, not a preference: it belongs to the server's own admission semaphore
    # (`Chemclaw3-mcp` `servers/calc`) and is declared here so a deployment that exceeds it fails
    # `Settings()` in every pod, naming both sides, instead of finding out under load. 0 declares no
    # ceiling, which makes the startup check and the alert inert — the same self-disabling
    # convention the other two budgets use, and the reason a dev run needs neither.
    #
    # The check covers the *durable* half only, which is the half that can be derived. The `calc`
    # bundle's own MCP server pods dispatch to the same backend from a tool call, with no
    # per-process cap to multiply, so the runtime pair — `sum(chemclaw_calc_requests_in_flight)`
    # against `chemclaw_calc_backend_max_concurrent_requests` — is what sees both.
    calc_fleet_worker_processes: int = Field(default=1, ge=0)
    calc_backend_max_concurrent_requests: int = Field(default=0, ge=0)

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
