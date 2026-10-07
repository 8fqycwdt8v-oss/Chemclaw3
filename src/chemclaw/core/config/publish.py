"""Publishing computed results outward: where sinks are discovered, and which are active.

Config says which and where; a sink's `sink.yaml` says what (URL, credentials, schema). Only
discovery, the enable list and the machinery's own bounds live here.
"""

import os

from pydantic import Field
from pydantic_settings import BaseSettings

from chemclaw.core.config.shipped import _shipped


class PublishSettings(BaseSettings):
    """Where result sinks are discovered, which are enabled, and what bounds the drain."""

    # OS-pathsep list of directories holding `sink.yaml` folders; read via `result_sinks_dirs`.
    # Earlier directories win a name collision.
    result_sinks_dir: str = Field(default_factory=lambda: _shipped("publish", "sinks"))

    # Comma list of discovered sinks to publish to. Empty by default: discovery is not enablement.
    # An undeclared name is a startup error.
    result_sinks: str = ""

    # Records per delivery; bounded because a batch is also the unit that is retried.
    result_publish_batch_size: int = Field(default=100, gt=0, le=10_000)

    # Drain cadence in minutes; it also runs on demand, so this is the worst-case wait.
    result_publish_schedule_minutes: int = Field(default=15, gt=0)

    # Bound on one `deliver` before the batch is left pending. Global so a manifest cannot grant
    # itself unlimited runtime.
    result_publish_timeout_seconds: float = Field(default=120.0, gt=0)

    # Budget for the republish walk, a full scan of never-pruned `calculation_results` and
    # `job_records`. Separate from the parent's budget so a retry can still happen inside it;
    # `_the_job_ceiling_covers_the_activity_it_bounds` keeps the job ceiling above it.
    result_republish_timeout_seconds: float = Field(default=14_400.0, gt=0)

    # Heartbeat timeout for the republish walk; `durable.heartbeat.beating` derives the beat
    # interval from it.
    result_republish_heartbeat_timeout_seconds: float = Field(default=300.0, gt=0)

    # Delivery failures a row survives before it stops being retried. It is never deleted; an
    # operator re-queues it with the backfill CLI.
    result_publish_max_attempts: int = Field(default=8, gt=0)

    @property
    def result_publish_lease_seconds(self) -> float:
        """How long a claimed outbox row stays out of the queue before it is claimable again.

        Equal to the drain activity's `start_to_close`: shorter lets a second drain steal rows still
        being delivered, longer delays rows a dead claimer abandoned. Read by `publish/outbox` and
        by `PublishResultsWorkflow`, so the lease and the budget cannot drift.
        """
        return self.result_publish_timeout_seconds * max(1, len(self.result_sink_list))

    @property
    def result_sinks_dirs(self) -> list[str]:
        """The discovery path, split. Read this rather than the raw field."""
        return [d for d in self.result_sinks_dir.split(os.pathsep) if d]

    @property
    def result_sink_list(self) -> list[str]:
        """The enabled sink names, split and stripped. Empty means publish nowhere."""
        return [s.strip() for s in self.result_sinks.split(",") if s.strip()]
