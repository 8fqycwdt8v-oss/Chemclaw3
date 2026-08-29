-- Who signed off on which revision of an experiment design, and why.
--
-- **This table exists because a docstring claimed it already did.** `protocols/store.advanced()`
-- demotes an `approved` design back to `draft` the moment a new revision lands, which is right —
-- an approval is a statement about a *document*, and a chemist who approved 80 °C did not approve
-- the 200 °C an agent drafted over it. The sentence excusing the cost of that demotion said "which
-- revision *was* approved stays recoverable: `set_status` records it". It did not. `set_status`
-- wrote one column on the header row and logged a line that did not even carry the revision
-- number, so after a demotion the question had no answer in the database at all.
--
-- **And `reason` was being thrown away in front of the person who typed it.** `POST
-- /protocols/{id}/status` accepts a `reason` up to 2,000 characters, `Chemclaw3_ui`'s
-- `ProtocolDocument` collects it from the chemist and sends it, and the route dropped it on the
-- floor and answered 204. "Abandoned — the starting material decomposes above 40 °C" is the single
-- most useful sentence anybody writes about a design, and it went nowhere.
--
-- Append-only by grant, like `experiment_protocol_revisions` and for the same reason: a credential
-- that could rewrite an approval is a credential that could forge one.

CREATE TABLE IF NOT EXISTS experiment_protocol_status_events (
    id          BIGSERIAL   PRIMARY KEY,
    design_id   TEXT        NOT NULL
                            REFERENCES experiment_protocols (design_id) ON DELETE CASCADE,
    -- The head revision at the instant the status was set. The whole point of the table: the
    -- header's `status` describes the head and moves with it, so only this column can say *which*
    -- document a person actually signed off on.
    revision    INTEGER     NOT NULL,
    status      TEXT        NOT NULL,
    -- Retained on offboarding, on `experiment_protocols.opened_by`'s line and more strongly: an
    -- approval with nobody attached to it is not a smaller record of an approval, it is a claim
    -- that one happened. `agent/leaver.py` states that position.
    actor       TEXT        NOT NULL DEFAULT '',
    reason      TEXT        NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT experiment_protocol_status_events_status_known
        CHECK (status IN ('requested', 'draft', 'approved', 'executed', 'abandoned'))
);

-- One design's sign-off history, newest first — what `GET /protocols/{id}` returns beside the
-- document so an approval is readable rather than merely stored.
CREATE INDEX IF NOT EXISTS experiment_protocol_status_events_design_idx
    ON experiment_protocol_status_events (design_id, id DESC);
