"""Publishing computed results outward: where sinks are discovered, and which are active.

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens every
section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators.

**This section is deliberately small, and the rule that keeps it that way is the package's own:**
*config says which and where; a manifest says what.* A field describing one destination — its URL,
its credentials, its schema — belongs in that sink's `sink.yaml`, not here. What lives here is the
discovery path, the enable list, and the machinery's own bounds, which are facts about this
deployment's Temporal and database budget rather than about any one sink.
"""

import os

from pydantic import Field
from pydantic_settings import BaseSettings

from chemclaw.core.config.shipped import _shipped


class PublishSettings(BaseSettings):
    """Where result sinks are discovered, which are enabled, and what bounds the drain."""

    # Where `sink.yaml` folders are discovered. An OS-pathsep list, like `PATH`, `connectors_dir`
    # and `data_sources_dir`; read through `result_sinks_dirs`, never raw. Earlier directories win
    # a name collision, so a deployment can mount a folder holding its own sink definition without
    # editing this repository.
    result_sinks_dir: str = Field(default_factory=lambda: _shipped("publish", "sinks"))

    # A comma list of discovered sink names to actually publish to. **Empty by default, and that is
    # the decision rather than an oversight**: discovery is not enablement (D-018), and a system
    # that started shipping every calculation to a destination nobody chose would be sending
    # science somewhere on a default. A name here that no manifest declares is a startup error.
    result_sinks: str = ""

    # How many records one delivery carries. Batched because every real destination is cheaper per
    # row in batches; bounded because a batch is also the unit that is retried, and an unbounded
    # one turns a single bad record into an unboundedly expensive retry.
    result_publish_batch_size: int = Field(default=100, gt=0, le=10_000)

    # How often the drain runs, in minutes. It also runs on demand, so this is the ceiling on how
    # long a result waits when nothing wakes it, not the expected latency.
    result_publish_schedule_minutes: int = Field(default=15, gt=0)

    # How long one `deliver` may take before the drain gives up on that batch and leaves its rows
    # pending. A ceiling on the machinery rather than on any sink, for the reason
    # `connector_job_timeout_seconds` is one global value: a manifest in this repository must not be
    # able to grant itself unlimited runtime.
    result_publish_timeout_seconds: float = Field(default=120.0, gt=0)

    # How long the *republish* walk may take — a full scan of `calculation_results` and
    # `job_records`, neither of which is ever pruned, so on a years-old deployment it is the one
    # multi-hour activity outside the calculators. Its own budget rather than the parent's: it ran
    # with `connector_job_timeout_seconds`, the same number `ConnectorJobWorkflow` gives the child
    # as an execution timeout, so both expired within milliseconds of each other, the activity's
    # `BAD_DATA_RETRY` could never reach a second attempt, and the run died as a bare
    # `WorkflowExecutionTimedOut` naming neither setting. `_the_job_ceiling_covers_the_activity_it
    # _bounds` now takes the max over this and `xtb_job_timeout_seconds`.
    result_republish_timeout_seconds: float = Field(default=14_400.0, gt=0)

    # How long Temporal waits to hear "still running" from that walk before declaring the worker
    # dead. Without it a worker killed ten minutes in was not noticed for the whole budget above;
    # `durable.heartbeat.beating` derives the beat interval from this number so the two cannot
    # drift. Same value and same reasoning as the calculators' own heartbeat timeout.
    result_republish_heartbeat_timeout_seconds: float = Field(default=300.0, gt=0)

    # How many delivery failures a row survives before it stops being retried. It is never deleted
    # — a row that has given up is the record of a destination that was down, and an operator
    # re-queues it with the backfill CLI once the cause is fixed.
    result_publish_max_attempts: int = Field(default=8, gt=0)

    @property
    def result_sinks_dirs(self) -> list[str]:
        """The discovery path, split. Read this rather than the raw field."""
        return [d for d in self.result_sinks_dir.split(os.pathsep) if d]

    @property
    def result_sink_list(self) -> list[str]:
        """The enabled sink names, split and stripped. Empty means publish nowhere."""
        return [s.strip() for s in self.result_sinks.split(",") if s.strip()]
