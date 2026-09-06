-- Drop the two `structure` columns nothing could ever fill.
--
-- **Why this is not additive, and why it is still safe.** `structure.atom_count` and
-- `structure.geometry` were created by 001 and were written by exactly two builders -- the subject
-- member and the conformer in `publish/dialect.py` -- both of which hardcoded `0` and `{}`.
-- Measured over all 19 published result shapes: 15 structure rows, one distinct value between
-- them, `(0, '{}')`. `PRESERVE_ON_BLANK` then treated those two as "the writer did not know", so
-- no later delivery could fill them either. There is no row in any deployment holding anything
-- else that this system wrote.
--
-- **A site that applied 001 must apply this**, because it is the same change as the writer's:
-- `geometry` is `NOT NULL` with no default there, so an image that no longer sends the column
-- would have every structure row refused. The reverse order is fine -- an older image sending
-- `geometry` to a database without the column has it omitted by the sink's `information_schema`
-- probe, with a warning, which is the schema-lag case that probe exists for.
--
-- On a fresh database this is a no-op: 001 no longer creates either column.
--
-- Where the two facts actually live: the coordinates in `calculation_payload`, which holds the
-- payload whole, and `atom_count` in `property_value` wherever a payload states one.

ALTER TABLE structure DROP COLUMN IF EXISTS geometry;
ALTER TABLE structure DROP COLUMN IF EXISTS atom_count;
