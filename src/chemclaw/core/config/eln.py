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
    # timeout bounds one batch of fetch+validate+index work.
    eln_export_dir: str = "data/eln-exports"
    eln_sync_timeout_seconds: float = Field(default=300.0, gt=0)
    # How long one `regex` transform may spend on one warehouse cell
    # (`D-2026-09-21-a-pattern-that-cannot-be-timed-out-is-run-by-an-engine-that-can`). A site
    # writes the pattern in its `datasource.yaml` and this repository runs it over free-text cells
    # whose length nobody here chose, so the work a match costs is unbounded in both factors and
    # `re` has no timeout at any of them. This is the bound, and it is a bound on the *work*: a
    # pattern that exceeds it fails the ingest naming itself, rather than being retried over the
    # same page.
    #
    # 0.25 s because it is four orders of magnitude above what a real pattern costs. Measured on
    # this box: a bounded pattern over a short cell is 1.2 us, and a full scan of a 1 MB cell with
    # no match is 0.24 ms — so the ceiling is ~1,000x the worst honest case and the shortest
    # catastrophic one tested reaches it in 0.25 s rather than never.
    eln_regex_timeout_seconds: float = Field(default=0.25, gt=0)
    # How long **every** `regex` transform together may spend on one page, which is the bound the
    # per-cell one above does not compose into. `warehouse/adapter._read` runs one match per
    # reaction field, per attribute, and per component and impurity *row*, so a page is
    # `eln_sync_batch_size x cells_per_entry` matches and the per-cell ceiling multiplies.
    #
    # **The reachable case is a pattern that is slow and *completes*, which is why the per-cell
    # bound cannot see it.** A pattern that exceeds 0.25 s is refused and, because
    # `PatternBudgetError` is in `durable/publish._BAD_DATA_TYPES`, ends the page after one cell. A
    # *polynomial* pattern never trips it: measured on this box, `a*a*a*$` over a 6,000-character
    # cell is **165 ms** — 66% of the per-cell budget, no refusal — and twenty such cells across a
    # 100-entry batch is **330 s**, which is past `eln_sync_timeout_seconds` (1.1x) and past the
    # heartbeat, after which the retry runs the identical page. `map_to_ord` is synchronous CPU
    # work, so no asyncio timer interrupts it; 1,818 of 2,000 cells were reached before the
    # activity's own deadline.
    #
    # **Half of `eln_sync_timeout_seconds`, and that is a split rather than a measurement.** The
    # page also writes, and nobody here has measured what that costs, so the regex half is given
    # half — enough that a refusal is *reported* by the activity instead of the activity being
    # killed, which is the whole gain over the status quo. It is not a tight bound and does not need
    # to be: an honest cell measured **0.472 ms**, so a whole honest page of 2,000 cells is 0.94 s
    # and this ceiling is ~160x it. The pathological page is refused at 150 s, naming how far it
    # got. Raising `eln_sync_timeout_seconds` without raising this only shrinks the regex share.
    eln_regex_page_budget_seconds: float = Field(default=150.0, gt=0)
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
    # per-entry writes fits comfortably inside `eln_sync_timeout_seconds` — it was sized against
    # per-entry PR-gate pushes, a cost `D-2026-08-25-an-eln-transcription-is-data-not-a-claim`
    # removed from this hop and nobody has re-measured without.
    eln_sync_batch_size: int = Field(default=100, ge=1)
    # How many chunks one *run* of the drain may take before it hands the rest to a fresh run with
    # `continue_as_new`. Nothing bounded this, and the ELN sync was the only drain in the package
    # without it: each chunk emits two activities, measured at 12.2 history events, so a first
    # backfill reached Temporal's 51,200-event ceiling at ~4,200 chunks — about 420,000 entries at
    # the batch size above, against a warehouse ELN sized at ~700,000 — and was *terminated*, which
    # is not a failure and so retries nothing and pushes nothing back. Same default and same
    # reasoning as `label_sync_max_iterations` and `document_sync_max_iterations`.
    # **Derived from `schedule_run_timeout_seconds` rather than chosen**, and it moved from 100
    # to 90 when that arithmetic was first done: `_a_bounded_run_fits_the_ceiling_that_kills_it`
    # refuses a count whose iterations cannot finish inside the `run_timeout` on the very run
    # they bound. At 100 its three-dispatch loop was 90,300 s against 86,400 s. What the cut costs
    # is one extra `continue_as_new`
    # per 90 iterations and nothing else — the hop carries the drain's position — and what it
    # buys is that a run large enough to use its budget is no longer killed near the end of one.
    eln_sync_max_iterations: int = Field(default=90, ge=1)
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
