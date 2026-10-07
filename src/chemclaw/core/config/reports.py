"""Settings for the report harness and sub-agent fan-out.

One domain section of the composed `Settings`; the package `__init__.py` flattens the sections and
owns the env prefix, `.env` loading and cross-section validators.
"""

from pydantic import Field
from pydantic_settings import BaseSettings


class ReportSettings(BaseSettings):
    """The report harness (plan Phase 5b) and sub-agent fan-out (F10-D).

    Grouped because both knobs govern durable fan-out work: a report's per-section activity
    budget and the concurrency bound on child workflows (report sections, memory-synthesis
    groups).
    """

    # Per-section retrieval budget for the durable development-report workflow — one section is one
    # activity, so a long report resumes section by section after a worker restart.
    report_section_timeout_seconds: float = Field(default=300.0, gt=0)
    # Bound on concurrent child workflows in one fan-out job (report sections, memory-synthesis
    # groups). Concurrency only; retries come from each child's own policy.
    orchestrator_max_parallel_children: int = Field(default=8, ge=1)
    # Wall-clock ceiling on one fan-out child, including its queue wait: a retry policy does not
    # bound a child that never fails nor finishes. `Settings` refuses a value below the section
    # budget, and `durable/publish.py::fan_out_queue_wait_timeout` derives the queue wait from what
    # is left.
    fan_out_child_timeout_seconds: float = Field(default=3600.0, gt=0)
