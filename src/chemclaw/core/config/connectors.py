"""The connector seam: which capability bundles this deployment runs, and how it reaches them.

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

import os

from pydantic import Field
from pydantic_settings import BaseSettings

from chemclaw.core.config.shipped import _shipped


class ConnectorSettings(BaseSettings):
    """The connector seam: which capability bundles this deployment runs, and how it reaches them.

    Its own section because a connector is the one mechanism for adding *any* capability — the
    MCP tools a FastAPI server serves, the durable jobs a Temporal worker runs, and the skills
    and agent profiles that come with them (`connectors/`,
    `docs/archive/plans/connector-plan.md`). It replaces the old `mcp_servers` list, which could
    only describe the first of those four.
    """

    # Where connector bundles are discovered: one or more directories, OS-path-separator
    # delimited like `PATH` (and like `skills_dir`), so an operator can add a private bundle
    # directory without code changes. A bundle is any subdirectory containing `connector.yaml`.
    # Read it through the `connectors_dirs` property, never raw. Earlier directories win a name
    # collision, so a private bundle can override a shipped one.
    connectors_dir: str = Field(default_factory=lambda: _shipped("connectors"))

    # Which discovered connectors are actually enabled — discovery is not enablement. Empty (the
    # default) means every discovered bundle, so a fresh checkout runs the full shipped surface.
    # A non-empty pathsep list narrows to exactly those names *in that order* (tool order is
    # part of the prompt, so it is configuration, not chance). A name here that no bundle
    # provides is a startup error rather than a capability that silently stops working.
    connectors_enabled: str = ""

    # Per-connector endpoint override, by connector name. A bundle's manifest ships a working
    # dev default (a loopback URL); a cluster's address belongs to the deployment, so Helm sets
    # this instead of patching a file in the repo. ENV override is JSON, e.g.
    # CHEMCLAW_CONNECTOR_URLS='{"molfp":"http://chemclaw-connector-molfp:8080/mcp"}'.
    connector_urls: dict[str, str] = Field(default_factory=dict)

    # What an unreachable enabled connector means. Default (`false`) is *degrade loudly*: the
    # service still starts, the failure is logged, reported by `/readyz` and counted by the
    # `chemclaw_connectors_unhealthy` gauge, and that connector's tools are simply not
    # reachable. `true` is fail-fast for a deployment where serving with a silently reduced tool
    # surface is worse than not serving at all (the fail-fast posture).
    connectors_required: bool = False

    # Bound on one connector's health probe on the *hot* path — every `/readyz`, every 10 seconds
    # per pod. Small deliberately: this is what the kubelet waits for, and the chart's
    # `probes.service.readiness.timeoutSeconds` is derived from it (`deployment-service.yaml`), so
    # it is the number that decides whether somebody else's outage takes a serving front door out
    # of its Service. It bounds each half whole — connect plus RPC, or connect plus read — rather
    # than each step in it; see `connectors/health.py`.
    connector_health_timeout_seconds: float = Field(default=2.0, gt=0)

    # The same probe at **startup**, where it is a one-time cost rather than a per-poll one.
    #
    # `check_connectors_at_startup` runs this sweep once and its verdict is final for the boot:
    # under `connectors_required` it decides whether the process serves at all. The poll's budget
    # is sized against a kubelet stopwatch and against a *cached* Temporal client; the first check
    # after process start has neither, and pays a cold connect — PEM files parsed, an mTLS
    # handshake — out of the same budget the `DescribeTaskQueue` RPC needs to come back. What that
    # costs is not a slow boot but a wrong one: the RPC runs out of budget, the sweep reports
    # `unknown`, and `unknown` neither counts in the gauge nor trips the gate — so a bundle whose
    # queue has no poller (a worker fleet at zero replicas, jobs accepted and never run) clears the
    # fail-fast posture that exists to refuse exactly that.
    #
    # 10 s: five times the poll's budget, and bounded by what a startup probe already grants — the
    # chart gives the front door 30 x 10 s to finish its lifespan (`probes.service.startup`), which
    # this is a small share of. Raising it delays a boot in the worst case and buys a verdict that
    # is right; the poll's 2 s cannot be raised for the same benefit without moving the kubelet
    # timeout with it, which is the coupling the derivation in the chart makes visible.
    connector_startup_health_timeout_seconds: float = Field(default=10.0, gt=0)

    # Bound on one connector's whole *open* — TCP dial, `initialize`, `tools/list` — per turn
    # (`connectors.transport.HeldConnectorSession.__aenter__`). Not redundant with the 5 s connect
    # timeout, which covers only the dial: the handshake is otherwise bounded by the session's
    # read timeout, sized for the slowest tool call (600 s for `calc`), so a connector that
    # accepted the socket and then went mute held every turn for that long before the first token.
    # Generous against a healthy fleet (a handshake is two round trips) and small against a turn.
    connector_open_timeout_seconds: float = Field(default=15.0, gt=0)
    # Bound on one connector session's close at turn teardown (`_shut_down`). Past it the holder
    # task is cancelled rather than awaited, so the slowest session close cannot hold the end of
    # every turn — the unbounded form made teardown hostage to a session that would not unwind.
    connector_teardown_timeout_seconds: float = Field(default=5.0, gt=0)

    # How long a verdict of "this connector is unreachable" is trusted, so a turn does not pay the
    # open bound above against a host already known to be down
    # (`D-2026-08-27-the-breaker-is-the-readiness-verdict-already-taken`).
    #
    # The state this reads is not new: `connectors.health` probes every enabled bundle at startup
    # and again on every `/readyz`, and until now the per-turn open path ignored it entirely — so a
    # dark connector cost `connector_open_timeout_seconds` on *every* turn for the whole outage,
    # with no backoff, while the answer was already sitting in the readiness snapshot.
    #
    # 30 s, and the number is a recovery bound rather than a savings one. Recovery has two paths and
    # this bounds the slower: the readiness sweep re-probes a recovered connector and records it
    # healthy, which readmits it on the next turn (≤ one kubelet interval plus
    # `service_readiness_cache_seconds`), and a deployment whose sweep does not run — the CLI, a
    # template activity in a worker — falls back to this window expiring and dialling for real. A
    # breaker with no recovery path amplifies the outage it was added for, so the expiry is the
    # half that must not be omitted.
    #
    # 0 disables it: every turn dials every connector, which is the behaviour before this existed.
    connector_breaker_window_seconds: float = Field(default=30.0, ge=0)

    # Whole-run **maximum** for one connector job's child workflow (`ConnectorJobWorkflow`).
    # Generous, because a connector job is by definition the long-running kind, but bounded so a
    # wedged connector workflow eventually fails instead of pinning a run forever.
    #
    # **A bundle may lower this for one of its own jobs and may not raise it**
    # (`JobSpec.timeout_seconds`, applied as a `min` by
    # `durable/connector_job.py::child_execution_timeout`). This used to be one global ceiling with
    # no per-manifest field at all, on the reasoning that a bundle in the repo must not be able to
    # grant itself unlimited runtime — which is right about the direction and was over-applied to
    # both. What it cost: one number bounds a twenty-second job and a four-hour job identically, so
    # with a bundle's worker down the short job sat `running` for the whole ceiling with nothing
    # said. A bound that can only move downward gives the deployment's call away in neither
    # direction (`D-2026-08-27-a-bundle-may-lower-its-own-ceiling`).
    #
    # It is a ceiling over the *whole* child, so it must exceed the longest activity that child
    # runs. `_the_job_ceiling_covers_the_activity_it_bounds` enforces that; raise this whenever you
    # raise that activity's budget.
    #
    # **The number is derived from the longest job this system runs, and that job changed.** It was
    # 90_000 — the 24 h DFT poll (`hpc_run_timeout_seconds`) plus an hour — and the DFT tier is gone
    # (`D-2026-08-26-semiempirical-is-the-whole-tier`). The longest child activity is now
    # `run_xtb_calculation`, a CREST search at `xtb_job_timeout_seconds` (4 h), so the default is
    # that plus an hour on the same reasoning: an hour of headroom rather than equality, because two
    # equal defaults make the ceiling the tighter of the two on the path the deployment runs.
    #
    # It covers **one attempt**, not `activity_max_attempts` of them — as the DFT-sized number did
    # not either. A job that exhausts its whole budget and is retried is bounded by this ceiling,
    # so the retry is cut short; a deployment that wants the retry budget to be reachable raises
    # this above `activity_max_attempts * xtb_job_timeout_seconds`.
    #
    # **It funds the queue wait as well as the work, which is what sets the default.** A bundle
    # activity's `schedule_to_start_timeout` is this ceiling's *headroom* —
    # `connector_queue_wait_timeout()` = this minus `longest_bundle_activity` minus
    # `activity_timeout_seconds` — because the wait precedes the work and the ceiling has to
    # contain both (`durable/publish.py` carries the arithmetic and what it replaced). So the
    # number is the sum of three measured quantities rather than one budget plus a round hour:
    #
    #     15,000  one CREST search at `xtb_job_timeout_seconds`, the longest activity a child runs
    #         30  the child's own overhead, `activity_timeout_seconds`
    #     10,170  the queue wait that leaves, ~1.4x the measured p95 backpressure on
    #             `connector-calc` at target load (p50 ~3,744 s, p95 ~7,128 s)
    #     ------
    #     25,200  = 7 h
    #
    # Lower it and the queue bound tightens with it, which is the honest direction: a site that
    # funds less runtime is also declaring less patience for a job that never gets a slot. Raise
    # it and `template_run_timeout_seconds` must rise too, or startup refuses the pair
    # (`_the_template_run_ceiling_covers_one_step`).
    connector_job_timeout_seconds: float = Field(default=25_200.0, gt=0)

    # Hard ceiling on a connector's request body, refused with 413 before anything reads it
    # (`connectors.server.connector_app`, `core.asgi.BodySizeLimit`). A connector's own setting
    # rather than reusing `service_max_request_bytes`: that one is sized for the front door's
    # multipart attachment upload, a shape a connector's `/mcp` never carries — every request there
    # is one MCP JSON-RPC call, whose arguments are chemistry-sized (a SMILES string, a job spec, a
    # batch of candidates), not a file. A smaller default follows from that difference in what a
    # legitimate request looks like, not from copying the front door's number. 0 disables, matching
    # the front door's knob.
    connector_max_request_bytes: int = Field(default=1_000_000, ge=0)

    # Bound on the record write every finished connector job performs (D-157). Small: it is one
    # upsert of a row the job has already earned, and a database that cannot take it in this long
    # is down — in which case the retries, and then the log line, are the right outcome.
    job_record_timeout_seconds: float = Field(default=30.0, gt=0)
    # How long the *model's* `get_durable_job_status` poll long-polls before answering `running`.
    # A poll from the model costs a whole conversation turn (connector open, graph compile, model
    # call), so answering `running` for a job finishing two seconds later spends another full turn
    # learning what this short wait delivers now. Temporal's own long-poll, never a sleep loop;
    # the HTTP job route deliberately does not wait (a browser's poll is cheap). Below the calc
    # bundle's 20 s inline wait, because this is a *re*-check, not the first wait. 0 disables.
    job_status_wait_seconds: float = Field(default=10.0, ge=0)
    # How many past runs `find_past_jobs` returns by default. Bounded because the results land in
    # the model's context: enough to recognise the campaign being looked for, not a table dump.
    job_record_search_limit: int = Field(default=20, ge=1)

    # Whether a discovered manifest may launch a subprocess (`endpoint: transport: stdio`).
    # **Default off, because a manifest is data and this field is the one that executes.** A
    # bundle directory is discovered by existing — any subdirectory of `connectors_dir` holding a
    # `connector.yaml` — and discovery is enablement unless `connectors_enabled` narrows it, so a
    # YAML file appearing on that path used to run its `command:` in the chat process, before the
    # MCP handshake, under the identity holding every connector token and the database pool. The
    # spawn happened even when the connector was then reported unreachable, which is what made it
    # quiet. No shipped bundle declares stdio; it is the zero-infrastructure path for local
    # development and for the transport's own tests, and those set this explicitly.
    connector_stdio_enabled: bool = False

    # Which jobs may declare `awaits_answer: true` and so run with **no wall-clock ceiling at all**
    # (`durable/connector_job.py::child_execution_timeout`), as `<bundle>.<job>` names separated by
    # the OS path separator.
    #
    # **The branch is right and the declaration needed a gate.** A job that suspends on a person
    # spends wall clock without doing work, and no finite ceiling is correct for it — a measured
    # campaign is `(n_rounds + 1)` waits and the shipped spec alone spans 154 days, so any ceiling
    # wide enough is a ceiling that reaps nothing. That argument is unchanged. What it does not
    # answer is *who decides a job has that shape*, and the answer was "whoever wrote the file":
    # `child_execution_timeout`'s own docstring states the invariant that "a manifest in this
    # repository still cannot grant itself runtime the operator did not fund", and this was the one
    # field that could. Measured — a manifest naming a bundle nobody vetted, no setting changed —
    # an 18,000 s fleet ceiling became `None`.
    #
    # Default off for a manifest, on for the one bundle that ships with the shape, which is the
    # same posture `connector_stdio_enabled` takes and for the same stated reason: a manifest is
    # data. The consequence a site accepts by adding a name here is the one
    # `child_execution_timeout` writes down — for that job the wall-clock reaper is gone, so a
    # bundle worker that never returns leaves the run `running` rather than failing it in hours.
    connector_jobs_awaiting_answer: str = "bo.start_optimization_campaign"

    @property
    def connector_jobs_awaiting_answer_list(self) -> list[str]:
        """The `<bundle>.<job>` names allowed to run without a wall-clock ceiling."""
        return [j for j in self.connector_jobs_awaiting_answer.split(os.pathsep) if j]

    @property
    def connectors_dirs(self) -> list[str]:
        """The connector bundle directories, split on the OS path separator (like `PATH`)."""
        return [d for d in self.connectors_dir.split(os.pathsep) if d]

    @property
    def connectors_enabled_list(self) -> list[str]:
        """The explicitly enabled connector names; empty means "every discovered bundle"."""
        return [c for c in self.connectors_enabled.split(os.pathsep) if c]

    # How long an irreversible effect's per-call approval stays open before the job gives up
    # (`D-2026-08-29-an-effect-declares-whether-it-can-be-undone`). Deliberately short beside the
    # durable wait's 90-day ceiling: an approval nobody answered in three days is a decision that
    # was not taken, and the arguments a chemist approved a week ago describe a situation that has
    # moved. An expiry fails the job and attempts nothing, which is the safe direction.
    effect_approval_deadline_days: float = Field(default=3.0, gt=0)
    #: The entitlement that may approve an irreversible effect — a role or security-group name, as
    #: `asked_of` carries elsewhere. **Empty is not "anybody"**: under `entra_required` a job
    #: declaring an irreversible effect refuses to run until a deployment names an approver, because
    #: an unrouted approval request is answerable by every authenticated principal, and the first
    #: version of this seam shipped exactly that — the requester could approve their own
    #: irreversible change. Off enforcement it stays open, as every other gate degrades in dev.
    effect_approval_role: str = ""
