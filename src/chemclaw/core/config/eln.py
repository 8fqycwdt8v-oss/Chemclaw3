"""ELN ingestion (plan Phase 4): the export adapters and the durable sync loop.

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

from pydantic import Field
from pydantic_settings import BaseSettings


class ElnSettings(BaseSettings):
    """ELN ingestion (plan Phase 4): the export adapters and the durable sync loop.

    Grouped because these knobs shape one ingestion pipeline: where the JSON/ORD exports land,
    how the cursor-driven sync batches/overlaps/heartbeats, and how often its Temporal Schedule
    fires. ELN-specific format lives only in the adapter, never in config (G6).
    """

    # The one concrete adapter reads a JSON-export ELN from this directory; the sync activity's
    # timeout bounds one batch of fetch+validate+index+PR-gate work.
    eln_export_dir: str = "data/eln-exports"
    eln_sync_timeout_seconds: float = Field(default=300.0, gt=0)
    # The sync fetches from this far *behind* its high-water cursor, so an export file that
    # lands late with an older payload timestamp (an upstream export-job retry) is still picked
    # up instead of being silently dropped forever. Re-fetching the window is safe and cheap
    # because ingestion is idempotent; one day covers routine export retries — anything later
    # needs a manual backfill (explicit `since`).
    eln_sync_overlap_seconds: float = Field(default=86400.0, ge=0)
    # An entry stamped further than this beyond the wall clock is rejected, not ingested: a
    # typo'd future year would otherwise become the persisted high-water cursor and silently
    # skip every later real entry (no code path ever lowers a stored cursor). One day tolerates
    # clock skew and timezone mishaps while catching implausible timestamps.
    eln_sync_future_tolerance_seconds: float = Field(default=86400.0, ge=0)
    # Bounds one sync activity attempt's *new* work: at most this many entries newer than the
    # cursor are ingested per attempt, and the workflow loops chunk by chunk, persisting the
    # advanced cursor after each one — so an arbitrarily large backlog makes bounded forward
    # progress instead of timing out one giant attempt forever. Entries inside the overlap
    # window re-ingest idempotently and do not count against the bound. Sized so a full chunk of
    # per-entry PR-gate pushes fits comfortably inside `eln_sync_timeout_seconds`.
    eln_sync_batch_size: int = Field(default=100, ge=1)
    # How many chunks one *run* of the drain may take before it hands the rest to a fresh run with
    # `continue_as_new`. Nothing bounded this, and the ELN sync was the only drain in the package
    # without it: each chunk emits two activities, measured at 12.2 history events, so a first
    # backfill reached Temporal's 51,200-event ceiling at ~4,200 chunks — about 420,000 entries at
    # the batch size above, against a warehouse ELN sized at ~700,000 — and was *terminated*, which
    # is not a failure and so retries nothing and pushes nothing back. Same default and same
    # reasoning as `label_sync_max_iterations` and `document_sync_max_iterations`.
    eln_sync_max_iterations: int = Field(default=100, ge=1)
    # Dead-worker detection for the (long-running) sync activity: it heartbeats while it
    # ingests, so Temporal notices a dead worker within this window instead of waiting out the
    # whole `eln_sync_timeout_seconds` start-to-close before retrying elsewhere.
    eln_sync_heartbeat_timeout_seconds: float = Field(default=60.0, gt=0)
    # A second concrete adapter reads native Open Reaction Database messages (human-readable ORD
    # JSON) from this directory — the "structured recipe" path, alongside the free-text JSON
    # export above. Same `ElnAdapter` contract, so both flow through the one sync loop.
    ord_export_dir: str = "data/eln-exports/ord"
    # Temporal Schedule cadence for the ELN sync (`durable/schedules.py`, applied by `make
    # schedules-apply`). The sync is self-cursoring (loads/stores its high-water mark in
    # `sync_cursors`), so its Schedule passes no argument. Schedules live in Temporal
    # (durability there, not host cron); overridable so a deployment tunes cadence without code
    # change.
    eln_sync_schedule_minutes: float = Field(default=60.0, gt=0)
