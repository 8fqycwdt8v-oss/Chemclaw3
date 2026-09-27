# D-2026-09-09-a-rebuild-nothing-counts-reports-as-finished — A rebuild nothing counts reports as finished

**Status:** accepted · **Date:** 2026-09-09 ·
**Implements:** the consequences `D-2026-09-09-a-map-number-is-not-a-molecule` names and does not
measure ·
**Extends:** D-005/D-011's "a persisted derived value carries the version that produced it",
`D-2026-08-25-a-label-is-derived-not-recorded` (a label is derived, so it has a producer version)

## Context

`D-2026-09-09-a-map-number-is-not-a-molecule` moved `STANDARDIZATION_VERSION` from `std6` to
`std7`. That decision is right and is not revisited here. Its own Consequences section says the
whole fingerprint index must be rebuilt and the whole label corpus re-derived, and calls this
"designed behaviour … whoever owns the drain should expect it rather than discover it".

Measured, the transition is worse than expected in two places, and both are the same shape: **the
population that is *not* current is invisible to every reader that reports currency.** Neither
half is reachable by any existing test, and both answer a chemist rather than an operator.

### 1. A partly re-indexed fingerprint index reports healthy

Driven against a live PostgreSQL through `find_similar_molecules`, 50 rows under `std6`:

```
A: 50 std6 rows, 0 std7   → WARNING "index is EMPTY: 0 records…"   hits 0  index_empty True
B: 50 std6 rows + 1 std7  → INFO    "1 record(s) indexed…"         hits 1  index_empty False
                            verdict "1 indexed molecule(s) matched this query."
```

**A is honest.** B is a deployment mid-rebuild, or one where a resumed ELN sync indexed a single
new reaction after the bump — and it is the failure `FingerprintSearch` exists to prevent, reached
through a definition change rather than through an unpopulated table. `index_is_empty` goes False
on the first rebuilt row, so the search stops saying "unanswerable" and a chemist gets a
confident, complete-looking "have we made this before?" answered over 2% of the corpus.

Nothing anywhere counted the other 98%. `store.count()`, `is_empty()` and `log_index_size` all
filter to the current definition — deliberately, and the constructor says why ("counting them
would report a populated index to an operator whose searches all return nothing"), which is the
right answer to a *different* question than the one an operator mid-rebuild is asking.

### 2. The re-label stamps un-derived rows as current, and coverage calls that COMPLETE

`ingest/labels/enrich.py` stamps every row it passes over with the current `labeller_version`,
including rows the labelling server answered for with neither half — and it must, or `stale()`'s
deterministic first batch is re-read forever, which is the wedge `reembed_stale` was changed to
prevent one index over. `merge` keeps whatever the previous labeller derived. So after a pass that
derived **nothing**:

```
coverage: 'COMPLETE: all 1 matching reaction(s) are labelled at the current version,
           so counts over this facet are totals rather than lower bounds.'
stored:   ('Oxidation', 'RXNO:1', 'smirks', 'srv:std7:v1')   # content derived under std6
stale under the new version: 0
```

Two docstrings — `enrich.py`'s module header and its own warning at the point of the stamp — said
in the present tense that "the coverage report counts them as unlabelled". `science/labels/store.py`
counts `labeller_version = %(version)s`, which counts them as **labelled**. On a Pistachio-scale
re-label, any window in which the labelling server is degraded becomes permanently invisible: the
rows claim currency under a standardization their content predates, and `stale()` will never
return them again.

## Decision

**One population is named in both indexes, and it is named where the answer is read.**

**Fingerprints — the superseded rows are counted, and a partial index says so in the verdict.**
`FingerprintStore` gains two methods, split for exactly the reason `is_empty` is already split
from `count`: `superseded_count()` is the operator's exact number, paid once per process beside
`count()`; `has_superseded_records()` is the searcher's boolean, required by the contract to
answer without a scan. `FingerprintSearch.index_partial` carries it into the payload and into
`verdict`, and `log_index_size` reports `PARTIAL` at WARNING with both numbers and the share.

**The search surface carries the boolean and not the number**, because it is read on every query
while the count is a `count(*)`. The number is the operator's and goes to the connector log, which
is where a re-index is run from anyway.

**`min(definition)`/`max(definition)` is the probe, not `WHERE definition <> … LIMIT 1`.** The
obvious form has to read every row before it can answer "none", which is precisely the *healthy*
case. The extremes bound every value in the column, so they are both this store's definition if
and only if nothing else is stored — and `<table>_definition_idx` (046) serves each as an O(log n)
`Limit`. Measured on PostgreSQL 16.15 at 200 000 rows, all current: **26.32 ms** against
**0.55 ms**, and only the first grows with the corpus.

**Labels — the stamp says which of the two things it means.** `store_labels` takes `derived`, and
a row nothing was derived for is stamped `underived_stamp(version)` rather than `version`.
`stale()` treats both forms as done, so the drain still advances; `coverage`, `select` and
`current_version` treat only the plain form as current, so the two `enrich.py` docstrings become
true. `labelled_at` is not advanced for such a row, because it is the time the content was
derived.

**A suffix on the one column, not a second column.** Staleness and currency are the same question
asked of the same value, and two columns is two places for that answer to drift. It is a *tagged*
value rather than free text: nothing outside `science/labels/store.py` composes or reads the tag,
and `_stamp` refuses a version that already carries it — which matters because the version string
is `f"{remote}:{STANDARDIZATION_VERSION}:{VOCABULARY_VERSION}"` over a separately versioned
server's own answer, so this repository does not get to constrain it at the source.

**`current_version()` skips the marked form in both backends.** It feeds every rxnfp tool, which
passes it straight to `coverage`/`select`; answering with a marked stamp would count only the rows
nothing was derived for — the original defect inverted. A corpus whose entire re-label found the
server down therefore has *no* current version, and the tools report it as unlabelled, which is
the honest answer.

**The `verdict` clauses compose in both arms.** The hits arm already carried the rule in a comment
("two independent facts, so two independent clauses — not two branches"); the no-hits arm below it
was still an `if`/`return` chain. No test could see it because the two facts it could shadow —
`scan_truncated` (set only by the substructure scan) and `approximate` (only by similarity search)
— cannot co-occur today. A third that co-occurs with both is what makes the shape matter, so both
arms now build one clause list.

## Consequences

**State A stays exactly as it was.** The instant of a definition bump is an index with nothing
searchable, and "SEARCH NOT RUN … an operator must populate it" is the right sentence for it. What
it gained is one appended clause on the operator's log line saying the rows are *there* and waiting
to be rebuilt rather than missing — a different action from re-ingesting from the source.

**Every similarity search pays one extra round trip.** `index_is_partial` is deliberately not
conditioned on the hit list the way `index_is_empty` is: emptiness only changes the reading of an
empty result, while a superseded fraction changes the reading of both. On the two tables that build
a `FingerprintSearch` this is the 0.55 ms probe above. `corpus_molecules`/`corpus_reactions` carry
no `definition` index and would seq-scan; neither builds a `FingerprintSearch` — `science/labels/
search.py` calls `find_matches` directly — so neither is on this path. **If that ever changes,
those two tables need the index before the probe is correct to run on them.**

**A substructure scan is not flagged partial, and that is not an omission.**
`find_substructure_matches` reads `all_records`, which is unfiltered by definition on purpose: a
stale-definition row's stored SMILES is still a correct substructure hit. That entry point searches
the whole table mid-rebuild, and saying otherwise would tell a chemist to distrust a complete
answer.

**A degraded re-label now shows up as PARTIAL or NOT ANSWERABLE YET rather than COMPLETE**, which
is a behavioural change an operator will see: a corpus that reported itself fully labelled before
this commit may report itself unlabelled after it. That is the defect, not a regression — the rows
whose stamp changes are exactly the rows whose content was never derived at the version they
claimed.

**There is still no re-index path, and this ADR does not build one.** The only writers of
`molecule_fingerprints`/`reaction_fingerprints` are `ingest/eln/ingest.py`, reached by re-running a
*cursored* sync whose `corpus_cursors` row has to be deleted by hand. Two things found while
deciding whether to build one here, both of which put it outside a change to these three modules:

- **A re-index cannot be derived from the index's own labels in general.** The stored label is the
  *previous* standardization's output, and standardization discards information, so re-standardizing
  it reproduces the old normalization's losses rather than the new rules. It happens to work for
  this bump (stripping atom maps from an already-standardized string is the same result) and would
  not for the next one. The source of record is the corpus, which makes a re-index an *ingest*
  concern rather than an index one.
- **The runtime role cannot clean up after itself.** `infra/sql/grants/app_privileges.sql` grants
  INSERT and UPDATE on both fingerprint tables and withholds DELETE, so a rebuild whose row *key*
  changes — a molecule whose standardized SMILES moved, which is exactly the mapped species this
  bump is about — inserts a new row and can never remove the old one. Those rows would then be
  counted, and warned about, forever.

So it is a register row with those two findings attached, not a module in this change. What this
commit does instead is make the state **discoverable**: an operator's log line names the number and
the share, and a chemist's answer says the index is mid-rebuild.

## Alternatives rejected

- **Count superseded rows on the search path.** The honest operator number is a `count(*)`; running
  it per query is the performance defect `is_empty` was written to avoid, one question over. The
  boolean is enough to stop a confident answer, and the number reaches the person who acts on it.
- **A `derived`/`derived_at` column for the label half.** Cleaner to read, and it needs a
  migration — which is another wave's file set — plus a backfill of every existing row. The honest
  form of it is not one boolean but two version columns (`attempted` and `derived`), because
  "derived at *some* version" is not the question `coverage` asks; that is a larger change with a
  second place for currency to drift. Recorded rather than dismissed: if a third reader of this
  distinction appears, the two-column form is the one to take.
- **Suppress the stamp for an un-derived row.** Removes the marker problem and re-introduces the
  wedge: `stale()` is deterministic, so the batch the server chokes on is re-read on every pass
  forever and nothing behind it is ever labelled.
- **Have `merge` clear the derived phase when nothing was derived.** Makes the row's content match
  its stamp, at the cost of deleting a previous labeller's work on the say-so of an outage — and
  `coverage` would still count it as labelled, because the count is on the stamp and not on the
  content.
- **Let `index_partial` displace the empty-index verdict.** State A is already correct and the two
  ask different things of the reader; a partial notice over an index that answered nothing at all
  would understate it.
