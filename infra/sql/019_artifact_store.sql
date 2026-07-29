-- Content-addressed store for a calculation's by-products (D-124).
--
-- `calculation_results` (001) holds the *answer* — a small JSON payload. Everything else a run
-- produced was deleted with its temporary directory, so a Hessian that cost minutes to compute
-- had to be recomputed to answer a second question about the same molecule. These two tables
-- keep those bytes.
--
-- **Two tables, deliberately.** The blob is addressed by the SHA-256 of its own uncompressed
-- content, so two runs that produced an identical geometry store one copy; the link row makes
-- that blob reachable *from* a calculation key and names the role it played. A future DFT
-- wavefunction or SCF restart file is another (calc_key, name) row over the same blob table —
-- nothing here is xTB-specific.
--
-- Applied by `make db-migrate` (idempotent).
CREATE TABLE IF NOT EXISTS artifact_blobs (
    content_hash   TEXT PRIMARY KEY,        -- sha256 hex of the UNCOMPRESSED bytes
    codec          TEXT        NOT NULL,    -- 'zlib' | 'none'
    byte_size      BIGINT      NOT NULL,    -- uncompressed length
    stored_bytes   BIGINT      NOT NULL,    -- length of `data` after the codec
    data           BYTEA       NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_access_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- We compress in the application, so TOAST must not spend a second, futile pass over bytes that
-- are already deflated. EXTERNAL stores them out-of-line, uncompressed by the server.
ALTER TABLE artifact_blobs ALTER COLUMN data SET STORAGE EXTERNAL;

CREATE TABLE IF NOT EXISTS calculation_artifacts (
    calc_key        TEXT NOT NULL,          -- CalculationKey.as_str() of the producing run
    name            TEXT NOT NULL,          -- the file's role: 'hessian', 'xtbopt.xyz', ...
    content_hash    TEXT NOT NULL REFERENCES artifact_blobs (content_hash) ON DELETE CASCADE,
    media_type      TEXT NOT NULL DEFAULT 'application/octet-stream',
    -- Wall time of the calculation that produced it: the cost of *not* having it, which is what
    -- the eviction sweep orders by. Cheap-to-regenerate blobs go first.
    compute_seconds DOUBLE PRECISION,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (calc_key, name)
);

-- `ON DELETE CASCADE` above is load-bearing: evicting a blob removes its link rows, so
-- `list_for` can never hand back a ref whose bytes are gone.
CREATE INDEX IF NOT EXISTS calculation_artifacts_content_idx
    ON calculation_artifacts (content_hash);

-- The eviction sweep's scan: idle blobs first.
CREATE INDEX IF NOT EXISTS artifact_blobs_idle_idx
    ON artifact_blobs (last_access_at);

-- The cost policy `workflows/retention.py` says a cache needs, and deliberately refused to invent
-- as an age cutoff: what one cached result was worth to compute. Recorded on every miss, so an
-- operator can ask what the cache has saved and the eviction sweep can order blobs by the cost of
-- regenerating them.
--
-- There is deliberately no `last_access_at` here. `calculation_results` is never evicted — the
-- JSON result is the *answer* (D-011, "never compute twice"), while a blob is a by-product from
-- which the answer can be regenerated, so only the by-product is evictable. That is what keeps
-- `workflows/retention.py`'s refusal literally true. A column nothing reads and nothing would
-- keep current is dead weight, and an access stamp on the cache-hit path is a write on the
-- hottest read in the system.
ALTER TABLE calculation_results
    ADD COLUMN IF NOT EXISTS compute_seconds DOUBLE PRECISION;
