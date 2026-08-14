"""The report harness (plan Phase 5b) and sub-agent fan-out (F10-D).

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

from pydantic import Field
from pydantic_settings import BaseSettings


class ReportSettings(BaseSettings):
    """The report harness (plan Phase 5b) and sub-agent fan-out (F10-D).

    Grouped because both knobs govern durable fan-out work: a report's per-section activity
    budget and the concurrency bound on child workflows (report sections, memory-synthesis
    groups).
    """

    # Per-section retrieval budget for the durable development-report workflow — one section is
    # one activity, so a long report resumes section by section after a worker restart.
    report_section_timeout_seconds: float = Field(default=300.0, gt=0)
    # A fan-out job (report sections, memory-synthesis groups) runs its independent sub-tasks as
    # child workflows; this bounds how many run at once so a large report/corpus does not spawn
    # hundreds of children simultaneously. Per-child retry + durability come from each child's
    # own retry policy; the bound is on concurrency only.
    orchestrator_max_parallel_children: int = Field(default=8, ge=1)
    # Wall-clock ceiling on one fan-out child. **A retry policy is not a ceiling**: `BAD_DATA_RETRY`
    # bounds how many times a child may *fail*, and a child that neither fails nor finishes — an
    # activity stuck on a dead dependency, a heartbeat that stops arriving — is retried zero times
    # and waited on forever, inside the parent's `asyncio.gather`. `ConnectorJobWorkflow` already
    # bounds its child this way (`connector_job_timeout_seconds`); the orchestrator's children had
    # nothing above them at all.
    #
    # An hour is generous against what these children do (one retrieval section, one note publish)
    # and is meant as a "this is stuck" bound rather than a service level. `Settings` checks it
    # against the section budget it has to contain, so lowering it below that budget is refused
    # rather than silently pre-empting work that was still running.
    fan_out_child_timeout_seconds: float = Field(default=3600.0, gt=0)
