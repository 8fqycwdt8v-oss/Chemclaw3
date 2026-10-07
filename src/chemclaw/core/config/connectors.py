"""The connector seam: which capability bundles this deployment runs, and how it reaches them.

One domain section of the composed `Settings`; the package `__init__.py` flattens the sections and
owns the env prefix, `.env` loading and cross-section validators.
"""

import os

from pydantic import Field
from pydantic_settings import BaseSettings

from chemclaw.core.config.shipped import _shipped


class ConnectorSettings(BaseSettings):
    """The connector seam: which capability bundles this deployment runs, and how it reaches them.

    A connector is the one mechanism for adding any capability: MCP tools, durable jobs, and the
    skills and agent profiles that come with them.
    """

    # OS-pathsep list of directories holding bundles (any subdirectory with `connector.yaml`); read
    # via `connectors_dirs`. Earlier directories win a name collision.
    connectors_dir: str = Field(default_factory=lambda: _shipped("connectors"))

    # Enabled connectors, pathsep-delimited and in tool order (order is part of the prompt). Empty
    # means every discovered bundle. An unknown name is a startup error.
    connectors_enabled: str = ""

    # Per-connector endpoint override by name; Helm sets it rather than patching a manifest. JSON in
    # the env, e.g. CHEMCLAW_CONNECTOR_URLS='{"molfp":"http://chemclaw-connector-molfp:8080/mcp"}'.
    connector_urls: dict[str, str] = Field(default_factory=dict)

    # Connectors served here by a stand-in (a deterministic test double), pathsep-delimited; empty
    # in production. A name makes `agent/tool_framing.py` mark every successful result from it as
    # coming from a double, so a fixed output is not read as a prediction. Read via
    # `connector_stand_ins_list`.
    connector_stand_ins: str = ""

    # What an unreachable enabled connector means: `false` degrades loudly (logged, `/readyz`,
    # `chemclaw_connectors_unhealthy`, its tools unreachable); `true` fails fast at startup.
    connectors_required: bool = False

    # Bound on one connector's health probe on the hot `/readyz` path; the chart derives the
    # kubelet's readiness `timeoutSeconds` from it. Bounds each half (connect plus RPC/read) whole;
    # see `connectors/health.py`.
    connector_health_timeout_seconds: float = Field(default=2.0, gt=0)

    # The same probe at startup, whose verdict is final for the boot. Larger than the poll's because
    # the first check pays a cold connect (PEM parse, mTLS handshake); too small a budget reports
    # `unknown`, which neither counts nor trips `connectors_required`. Fits within the chart's
    # startup probe window.
    connector_startup_health_timeout_seconds: float = Field(default=10.0, gt=0)

    # Bound on one connector's whole open per turn (dial, `initialize`, `tools/list`). The connect
    # timeout covers only the dial; without this a mute server holds the turn for its read timeout.
    connector_open_timeout_seconds: float = Field(default=15.0, gt=0)
    # Bound on one session's close at turn teardown; past it the holder task is cancelled.
    connector_teardown_timeout_seconds: float = Field(default=5.0, gt=0)

    # How long an "unreachable" readiness verdict is trusted, so a turn skips the open bound against
    # a host already known to be down. A recovery bound: the readiness sweep readmits a recovered
    # connector sooner, and where no sweep runs (CLI, worker) this expiry is the only recovery path.
    # 0 disables it.
    connector_breaker_window_seconds: float = Field(default=30.0, ge=0)

    # Whole-run maximum for one connector job's child workflow (`ConnectorJobWorkflow`), covering
    # one attempt. A bundle may lower it for its own job (`JobSpec.timeout_seconds`, applied as a
    # `min`) and may not raise it. It must exceed the longest child activity
    # (`_the_job_ceiling_covers_the_activity_it_bounds`), and its headroom is the activity's queue
    # wait (`connector_queue_wait_timeout()`; arithmetic in `durable/publish.py`):
    #
    #     15,000  one CREST search at `xtb_job_timeout_seconds`
    #         30  the child's overhead, `activity_timeout_seconds`
    #     10,170  queue wait, ~1.4x the measured p95 backpressure on `connector-calc` ------ 25,200
    #     = 7 h
    #
    # Raising it requires raising `template_run_timeout_seconds`
    # (`_the_template_run_ceiling_covers_one_step`).
    connector_job_timeout_seconds: float = Field(default=25_200.0, gt=0)

    # Total wait plus run for a queued tool call (`schedule_to_close` of its activity,
    # `connectors/queued_workflow.py`). An hour: a call still queued after that is load the
    # deployment is not sized for. The in-turn wait is each endpoint's `queued.inline_wait_seconds`.
    queued_tool_timeout_seconds: float = Field(default=3_600.0, gt=0)
    # Ceiling on the pause between two asks of a full server; seconds, because the dispatching
    # worker is sized to the server's slots.
    queued_tool_retry_max_seconds: float = Field(default=10.0, gt=0)
    # Re-sends of a queued call after a fault other than a full server (a rolling pod, a transport
    # error); bounded so an outage is reported promptly.
    queued_tool_fault_attempts: int = Field(default=3, ge=1)
    # How often a waiting turn asks where its queued call is ("queued"/"running",
    # `connectors/queued.py::_wait_reporting`). On a server without task-queue stats the count is
    # omitted and the backlog is no longer read.
    queued_tool_progress_seconds: float = Field(default=2.0, gt=0)

    # Ceiling on a connector's request body, refused with 413 before reading
    # (`core.asgi.BodySizeLimit`). Smaller than the front door's because an `/mcp` request is one
    # JSON-RPC call, never a file upload. 0 disables.
    connector_max_request_bytes: int = Field(default=1_000_000, ge=0)

    # Characters of one tool's description carried per model call. A description is untrusted text
    # from the server's `tools/list`, sent ahead of the system message on every call; bounding it
    # per tool (the manifest bounds the tool count) keeps an out-of-tree server from setting
    # per-turn spend. 6,000 is about twice the largest real description; past it the text is cut
    # head-and-tail with a notice and a WARNING. 0 disables.
    connector_max_tool_description_chars: int = Field(default=6_000, ge=0)

    # Bound on the record write a finished connector job performs (one upsert).
    job_record_timeout_seconds: float = Field(default=30.0, gt=0)
    # How long the model's `get_durable_job_status` long-polls (Temporal's long-poll) before
    # answering `running`, since each model poll costs a whole turn. Below the calc bundle's inline
    # wait; the HTTP route does not wait. 0 disables.
    job_status_wait_seconds: float = Field(default=10.0, ge=0)
    # Default number of past runs `find_past_jobs` returns; results land in the model's context.
    job_record_search_limit: int = Field(default=20, ge=1)

    # Whether a discovered manifest may launch a subprocess (`endpoint: transport: stdio`). Off by
    # default: a manifest is data, discovery is enablement, and a spawn would run in the chat
    # process with every token and the database pool. No shipped bundle uses stdio; dev and
    # transport tests set it.
    connector_stdio_enabled: bool = False

    # Top-level packages a manifest may name in an imported-and-called field (`params_model`,
    # `precondition`, `unavailable_reason`, `ingest`, `retrieve`, `commitments`, `driver`). Comma
    # separated; `chemclaw` is always allowed. Same reasoning as `connector_stdio_enabled`:
    # importing a module runs its code in-process, and a third-party driver is one deliberate
    # operator setting.
    manifest_driver_packages: str = ""

    @property
    def manifest_driver_package_list(self) -> frozenset[str]:
        """The packages a manifest may import from, always including this tree's own.

        Stripped and empties dropped: a stray space would silently withhold an entitlement.
        """
        named = (part.strip() for part in self.manifest_driver_packages.split(","))
        return frozenset({"chemclaw", *(part for part in named if part)})

    # Jobs (`<bundle>.<job>`, pathsep-separated) allowed to declare `awaits_answer: true` and run
    # with no wall-clock ceiling (`durable/connector_job.py::child_execution_timeout`). A job
    # waiting on a person has no correct finite ceiling, but a manifest must not grant itself
    # unfunded runtime, so the operator lists it. For a listed job a worker that never returns
    # leaves the run `running`.
    connector_jobs_awaiting_answer: str = "bo.start_optimization_campaign"

    @property
    def connector_jobs_awaiting_answer_list(self) -> list[str]:
        """The `<bundle>.<job>` names allowed to run without a wall-clock ceiling.

        Stripped, because a stray space would make `require_funded_ceiling` refuse a shipped grant.
        The `os.pathsep` separator is kept for compatibility.
        """
        entries = self.connector_jobs_awaiting_answer.split(os.pathsep)
        return [name for name in (entry.strip() for entry in entries) if name]

    @property
    def connectors_dirs(self) -> list[str]:
        """The connector bundle directories, split on the OS path separator (like `PATH`)."""
        return [d for d in self.connectors_dir.split(os.pathsep) if d]

    @property
    def connector_stand_ins_list(self) -> list[str]:
        """The connectors this deployment serves from a stand-in rather than the real capability."""
        return [c.strip() for c in self.connector_stand_ins.split(os.pathsep) if c.strip()]

    @property
    def connectors_enabled_list(self) -> list[str]:
        """The explicitly enabled connector names; empty means "every discovered bundle"."""
        return [c for c in self.connectors_enabled.split(os.pathsep) if c]

    # Days an irreversible effect's per-call approval stays open before the job gives up; short,
    # since an old approval describes a situation that has moved. Expiry fails the job and attempts
    # nothing.
    effect_approval_deadline_days: float = Field(default=3.0, gt=0)
    #: The role or security group that may approve an irreversible effect. Empty is not "anybody":
    #: under `entra_required` such a job refuses to run until an approver is named, since otherwise
    #: the requester could approve their own change. Without enforcement it stays open, as in dev.
    effect_approval_role: str = ""
