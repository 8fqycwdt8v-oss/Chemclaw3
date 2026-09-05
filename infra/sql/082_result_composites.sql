-- The durable record of a *tool* composite, which had none
-- (D-2026-09-05-a-composite-with-no-record-cannot-be-republished).
--
-- Publishing is recoverable because every published shape has a local record behind it: a
-- primitive is a `calculation_results` row and `publish/backfill.backfill_cached` walks it, a job
-- composite is a `job_records` row and `backfill_jobs` walks that. A **tool** composite —
-- `compute_thermochemistry`, `predict_logd` — is in neither by construction: its key would name
-- its own output, so it is not written to the calculation cache (D-011,
-- `D-2026-08-16-the-physics-leaves-the-cache-stays`), and no Temporal job produced it.
--
-- The consequence was that the tool hook's enqueue was the *only* copy. `publish/outbox.enqueue`
-- swallows every failure by construction — a completed calculation must not be failed by a queue
-- write — so a local database blip, or a sink attached a month after the work was done, lost the
-- composite permanently: nothing in this deployment could produce it a second time without
-- re-running the science.
--
-- This table is that missing record, and `publish/backfill.backfill_composites` is the walk over
-- it. Written **before** the enqueue and independently of whether any sink is enabled, for the
-- same reason `cli/backfill_publications` exists at all: the corpus computed before a results
-- store was attached is the more valuable half.
--
-- Keyed by the composite's own `calc_ref`, which `publish/hooks._composite_ref` content-addresses
-- on the result — so the same question asked twice is one row, and the same question after the
-- science moved is two. Never pruned: it is a record, and `durable/retention._NOT_PRUNED` says so
-- beside `calculation_results` and `job_records`.
CREATE TABLE IF NOT EXISTS result_composites (
    calc_ref     TEXT        PRIMARY KEY,
    -- `<connector>.<tool>`: a route, exactly what the hook stamps as `calc_type`.
    calc_type    TEXT        NOT NULL,
    -- The result model's own name, which is the only thing that routes a composite to a projector
    -- (`D-2026-08-26-a-route-is-not-a-shape`). Without it a walk over this table would skip every
    -- row, which is the defect migration 055 fixed for `job_records`.
    payload_kind TEXT        NOT NULL DEFAULT '',
    -- What was asked for, as opposed to what came back. Kept because it is the record's own
    -- account of the request and it is what a person reads when the answer looks wrong.
    input_hash   TEXT        NOT NULL DEFAULT '',
    payload      JSONB       NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The walk's order, so an interrupted backfill has made contiguous progress. `calc_ref` breaks
-- ties on `created_at`, which is not unique — the same total-order argument `publish/backfill`
-- already writes down for `calculation_results` and `job_records`, where a tied row could land on
-- a page boundary and be fetched by neither page.
CREATE INDEX IF NOT EXISTS result_composites_walk ON result_composites (created_at, calc_ref);
