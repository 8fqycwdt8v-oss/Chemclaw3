-- The ingest rejection ledger: a refused record kept as an answerable row, not only a log line
-- (D-2026-08-27-a-refused-record-is-a-question-somebody-will-ask).
--
-- A record an ingest source offers and this system refuses leaves a WARNING and nothing else. The
-- seeded corpus has exactly one such entry — a well logged at 119.43% yield, refused because
-- `OrdReaction` bounds a yield at 100 — and the only thing a chemist asking about it could be told
-- was "I have no such record", which is true of the corpus and false of what the system knows. The
-- better answer exists in a log file nobody can query: the entry was seen, it was refused, and the
-- reason names the defect.
--
-- **A ledger, not a second log**, which is what the key decides. `(source, entry_id)` is the
-- primary key, so a record refused on every sync run is one row with a moving `last_seen` and a
-- rising `occurrences` — the shape that answers "is this still happening, and since when" without
-- growing. A log-shaped table keyed on an event id would answer the same question by making the
-- reader count rows, and would grow without bound while doing it.
--
-- **Growth is bounded here rather than by a retention sweep** — `ingest/rejections.py` keeps at
-- most `_MAX_ROWS_PER_SOURCE` rows per source, evicting the least recently refused inside the same
-- transaction as the write. A corpus with one systematically broken field is precisely the case
-- that would otherwise write millions of rows, and it is the case in which the *newest* refusals
-- are the informative ones: an aged-out row is a defect nothing has re-offered since. That is why
-- the runtime role is granted DELETE on this table alone among the ingest tables, and why nothing
-- in `durable/retention.py` prunes it.
CREATE TABLE IF NOT EXISTS ingest_rejections (
    -- The registry source name that offered the record (`CHEMCLAW_DATA_SOURCES`), never the
    -- adapter class: two ELNs may legitimately key their rows alike, and the source is what tells
    -- a reader whose data quality this is a statement about.
    source      TEXT        NOT NULL,
    -- The entry id as the source spelled it. For a file-export adapter refusing a file it could
    -- not read at all, the file stem — the only id that exists when the payload never parsed.
    entry_id    TEXT        NOT NULL,
    -- Why it was refused, in the words of the refusal itself. This is the whole value of the row:
    -- "a yield cannot exceed 100%" is what turns "no such record" into an answer. It is also
    -- external text — `str(exc)` over a record an export wrote, and a `ValidationError` renders
    -- the offending `input_value=` verbatim — so it is truncated by the writer and, on the one
    -- path that shows it to a model (`agent/research_tools.py::_refused_on_ingest`), wrapped in
    -- the data envelope rather than merely neutralised. This comment said "neutralised" alone
    -- while the reader only stripped envelope delimiters, which stops a forged delimiter and does
    -- nothing to a payload that spells none.
    reason      TEXT        NOT NULL,
    -- When this record was first refused, and when it last was. The pair is what separates "one
    -- bad export last March" from "every run, still".
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- How many times it has been refused. Not derivable from the two timestamps, and it is what
    -- distinguishes a record re-offered hourly from one seen twice a year.
    occurrences BIGINT      NOT NULL DEFAULT 1,
    PRIMARY KEY (source, entry_id)
);

-- The eviction reads a source's rows newest-first and the reader orders its matches the same way,
-- so both plan onto this rather than sorting the source's whole set in memory.
CREATE INDEX IF NOT EXISTS ingest_rejections_recent_idx
    ON ingest_rejections (source, last_seen DESC);
