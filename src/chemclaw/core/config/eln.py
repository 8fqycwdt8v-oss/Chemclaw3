"""Settings for ELN ingestion: the export adapters and the durable sync loop.

One domain section of the composed `Settings`; the package `__init__.py` flattens the sections and
owns the env prefix, `.env` loading and cross-section validators.
"""

from pydantic import Field
from pydantic_settings import BaseSettings


class ElnSettings(BaseSettings):
    """ELN ingestion (plan Phase 4): the export adapters and the durable sync loop.

    Grouped because these knobs shape one ingestion pipeline: where the JSON/ORD exports land,
    how the cursor-driven sync batches/overlaps/heartbeats, and how often its Temporal Schedule
    fires. ELN-specific format lives only in the adapter, never in config (G6).
    """

    # Directory the JSON-export adapter reads; the sync timeout bounds one batch of
    # fetch+validate+index work.
    eln_export_dir: str = "data/eln-exports"
    eln_sync_timeout_seconds: float = Field(default=300.0, gt=0)
    # Work bound on one `regex` transform over one warehouse cell (`re` has no timeout, and both the
    # site's pattern and the cell are unbounded). Exceeding it fails the ingest naming the pattern
    # rather than retrying the page. 0.25 s is ~1000x the worst honest case.
    eln_regex_timeout_seconds: float = Field(default=0.25, gt=0)
    # Matching budget for all `regex` transforms on one page, which the per-cell bound does not
    # compose into: a slow pattern that completes under the per-cell bound, multiplied across a
    # page, would outlast the activity and retry the same page forever. Half of
    # `eln_sync_timeout_seconds`, so a refusal is reported by the activity rather than the activity
    # being killed. Counts matching time only (`expr._PageBudget`), not fetches or writes.
    eln_regex_page_budget_seconds: float = Field(default=150.0, gt=0)
    # How far behind its high-water cursor the sync re-fetches, so a late export with an older
    # timestamp is still picked up. Safe because ingestion is idempotent; later arrivals need a
    # manual backfill (explicit `since`).
    eln_sync_overlap_seconds: float = Field(default=86400.0, ge=0)
    # Entries stamped further than this into the future are rejected: one would become the persisted
    # cursor and skip every later real entry (no path lowers a stored cursor).
    eln_sync_future_tolerance_seconds: float = Field(default=86400.0, ge=0)
    # Bound on new entries ingested per sync attempt; the workflow loops chunk by chunk, persisting
    # the cursor after each, so a large backlog makes bounded progress. Overlap re-ingests do not
    # count. Sized to fit inside `eln_sync_timeout_seconds`.
    eln_sync_batch_size: int = Field(default=100, ge=1)
    # Chunks per run before `continue_as_new`, bounding event history. Derived from
    # `schedule_run_timeout_seconds`: `_a_bounded_run_fits_the_ceiling_that_kills_it` refuses a
    # count whose iterations cannot finish inside the run timeout.
    eln_sync_max_iterations: int = Field(default=90, ge=1)
    # Heartbeat timeout for the sync activity, so a dead worker is noticed before the whole
    # start-to-close lapses.
    eln_sync_heartbeat_timeout_seconds: float = Field(default=60.0, gt=0)
    # Directory the ORD adapter reads (native Open Reaction Database JSON messages); same
    # `ElnAdapter` contract and sync loop as the JSON export.
    ord_export_dir: str = "data/eln-exports/ord"
    # Temporal Schedule cadence for the ELN sync (`durable/schedules.py`). The sync is
    # self-cursoring (`sync_cursors`), so its Schedule passes no argument.
    eln_sync_schedule_minutes: float = Field(default=60.0, gt=0)
