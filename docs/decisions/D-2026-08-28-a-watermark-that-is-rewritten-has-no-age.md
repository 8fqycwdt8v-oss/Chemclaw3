# D-2026-08-28-a-watermark-that-is-rewritten-has-no-age — a cursor is stored when it moves, and four claims the feed change left wrong

A review of `D-2026-08-28-a-feed-is-a-corpus-that-does-not-stop` after it merged raised six findings.
One is refuted below and is worth recording as such; the other five are real, and one of them is a
behaviour change that narrows a sentence in that ADR. This is the superseding record, because a
merged ADR is not edited.

## 1 — The cursor was written every page, which left `updated_at` meaningless

`D-2026-08-28-a-feed-is-a-corpus-that-does-not-stop` says, of the position it persists:

> **Written every page, not only the last.** … An empty position is never stored: a pass that
> advanced past nothing has nothing to resume after.

The empty guard is the wrong bound, and the gap between the two is exactly the failure case. **A
stalled feed does not return an empty position.** It returns the same non-empty key it resumed
from — the source stopped exporting, the page is empty, `drain_corpus` returns `cursor=after` — so
`store_corpus_cursor` accepted it and `ON CONFLICT DO UPDATE … updated_at = now()` re-stamped the
row. On every fire, forever.

That costs nothing in correctness — the position is the same value — and it destroys the only
signal this table could carry. `ingest/labels/cursor.py` states plainly that a stalled feed has no
first-party detector today and names a staleness gauge over `updated_at` as one of the two shapes
that would close it; `072_corpus_cursors.sql` describes the column the same way. Written
unconditionally, that gauge could never fire: the column would read "synced seconds ago" on a feed
whose source had been dead for a month.

**Decision: the store is gated on `report.advanced`.** The field already exists — the same one the
workflow's "no cursor advance" warning reads, computed in `drain_corpus` where both the resumed
position and the reached one are in scope. So `corpus_cursors.updated_at` now answers *when this
feed last moved*, which is the only question worth asking of a watermark's age.

What this gives up is nothing: re-writing an unchanged position was a no-op with a side effect.
What it does not give up is the resume guarantee — a page that *did* advance still stores
immediately, so a run interrupted between pages resumes where it stopped rather than where the
previous run left off.

The honest residual, unchanged by this: a genuinely quiet source and a broken one still look alike
in that column. Every watermark has that property — `ingest/eln/cursor.py`'s lag gauge says so of
its own — and the answer is a threshold set against the feed's cadence, not a different column.
`tests/test_corpus_cursor.py` pins the gate in both directions, which the first version had no test
for at all.

## 2 — A test that proved the key never exercised it

`test_corpus_reactions_is_searchable_with_no_search_code_of_its_own` is the one test that drives the
real table, and it wrote `id="pistachio:s1"` with no source — a leftover from the draft that
composed `<source>:<reaction_id>` into one string before migration `063` existed. So the assertion
that the table is `(source, id)`-keyed was made by a test that populated neither half of that key,
and left `source=''` rows behind it.

It now writes the same entry id under two sources with different chemistry and asserts both rows
survive. That is the collision the pair key exists to prevent, and nothing weaker demonstrates it.

The membership assertion reads `all_records` rather than the similarity hits, deliberately: the
second row is legitimately below the similarity floor, so asserting it through a ranked search would
have proved the threshold works and quietly stopped proving the key does.

## 3 — Three docstrings describing code that was not shipped

The rework from the composed id to `(source, id)` left references to `corpus_reaction_id` (deleted),
to a `source || ':' || reaction_id` SQL predicate (never merged — the shipped narrowing is an
`unnest(sources, ids)` zip) and to `_fingerprint_reaction` (renamed `_collect_fingerprint` when the
write was batched). All three are in test docstrings, which is where they are least likely to be
caught and most likely to be believed: a test's docstring is read as a statement about the system.

## 4 — A parity claim between two backends that differ

`InMemoryFingerprintStore.add_many`'s docstring justified delegating to `add` as keeping "the two
backends' contents" from diverging on the unsourced-twin supersede. They already diverge on exactly
that, by decision: the in-memory store pops the twin, and `PostgresFingerprintStore` deliberately
does not, because the runtime role holds no `DELETE` on that table and an unsourced row is not
reliably a twin. The delegation is still right — one definition of *this* class's rule — and the
reason given for it was not.

## 5 — Refuted: the narrow exception catch

The review held that `_collect_fingerprint` catching only `FingerprintInputError` lets a species
that makes `standard_smiles` raise abort a whole page, which the activity would then retry forever.

**Measured, and it does not.** `standard_smiles` is documented as lenient — "the standardized
canonical SMILES, or the input unchanged if it does not parse … one odd label must not abort
ingestion" — and has no raise path. Eight hostile species (`C(((C`, `[`, `Q1QQ`, `C%%%`, `\`,
`[Xx]`, `c1ccccc`, `C1CC`) each went through `transformation_of` → `record_for_reaction` and
produced bits, raising nothing at all. RDKit logs a parse error and the pipeline carries the raw
string.

Recorded rather than dropped, because the finding is plausible from the code — `_species` one
function over *does* guard `InvalidSmilesError` — and the next reader will have the same doubt. The
guard there is defensive against a raise this path cannot produce; that is pre-existing and is not
touched here.

## What did not change

The decision in `D-2026-08-28-a-feed-is-a-corpus-that-does-not-stop` stands entirely: a second
`corpus_reactions` table on `054`'s citation argument, `(source, id)`-keyed, DRFP over
`reactants>>products`, `append_only` as the binding author's claim, the position in its own table,
and `conditions_for_similar_reaction` as its reader. Only the sentence quoted at the top narrows.
