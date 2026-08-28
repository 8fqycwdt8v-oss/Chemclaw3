# D-2026-08-28-a-feed-is-a-corpus-that-does-not-stop — an append-only reaction source keeps its keyset position, and every corpus reaction gets a DRFP row

A daily job is to pull `(reaction_id, reaction_smiles)` out of an external ELN database. The
reactions stay there; the **vectorization — of the reaction and of every individual molecule in it —
has to happen here**, because that is what makes them searchable by this system at all.

Asked of `HEAD` rather than of the documents, most of that already worked. The shape is a `corpus:`
binding, not an `ingest:` one — the reaction arrives already assembled as
`reactants>agents>products`, which is exactly what `CorpusBinding` is for.
`ReactionCorpusWorkflow` already has a daily Schedule (`corpus_sync_schedule_minutes`, 1440),
`drain_corpus` already splits the species into `reaction_species` with their roles, and
`corpus_molecules` already gets ECFP4 and pattern bits per distinct structure. **Molecule
vectorization was never the gap.**

Two things were.

## 1 — The reaction itself was never fingerprinted

`record_for_reaction` — the DRFP write — has exactly one caller in the tree, `ingest/eln/ingest.py`.
`drain_corpus` writes none. So a bulk reaction source arrived searchable by *structure* and never by
*transformation*: `similar_reactions` could not see a single row of it, and nothing said so.

This is the same absence `D-2026-08-25-a-corpus-is-evidence-not-an-eln` created deliberately for the
*transcription* tier and did not intend for the fingerprint one. A corpus writes no
`reaction_records` because a patent is not this organisation's run — that argument is untouched. It
says nothing about bits.

### Decision: a second table, `corpus_reactions`, not more rows in `reaction_fingerprints`

The same case `054_corpus_molecules.sql` makes for the molecule half, and stronger here.
`reaction_fingerprints.id` is the **bare** reaction id. `ingest_reaction`'s own docstring already
records the consequence — "this function writes four indexes and only three of them can tell the two
sites apart" — and `D-2026-08-26-a-transcription-is-keyed-by-its-source` fixed the same defect one
table over. Pouring a feed of millions of rows into it would:

* collide with this organisation's own ELN runs on any shared entry id, silently, and
* swamp `similar_reactions` with hits whose `reaction-<id>` citation resolves to a different record —
  the four-orders-of-magnitude argument `molecules.py` already makes, on the index where the citation
  is *load-bearing* rather than decorative.

`corpus_reactions.id` is `<source>:<reaction_id>`, so a hit joins to `reaction_labels
(source, reaction_id)` by construction.

The table carries the same five columns as `003`, so `PostgresFingerprintStore` serves similarity
over it with **no new search code at all** — the property `corpus_molecules` was built for, applied
to the other half. `tests/test_reaction_corpus.py` drives that against the real database rather than
the in-memory double, because the claim is about a migration and an HNSW index and a doubled store
cannot exercise either.

### The bits are taken over `reactants>>products`, agents dropped

`DrfpEncoder` folds the agent slot back onto the reactants (`sides[0] += "." + sides[1]`), so a
fingerprint built from the three-part form encodes the solvent as part of the transformation. That
was measured on the ELN path and is why `transformation_smiles` exists: one coupling in THF against
the same coupling in 2-MeTHF scored **0.82**, and **1.00** once the agents were excluded. It is also
what makes `reaction_definition()`'s own `agents-excluded` token true of these rows instead of a
claim about them.

Nothing is lost. Every agent is a row in `reaction_species` carrying its role, which is the index
built to answer *which solvent, which ligand, which base* — and `051`'s header says so in as many
words.

The stored **label** is the corpus's verbatim `record_smiles`, not a standardized rendering, which
differs from `reaction_fingerprints` and is deliberate: the corpus tier keeps what was recorded
("what is displayed should be what was recorded"), and a label disagreeing with the row it came from
is worse than one that does not match another table's spelling. The bits are unaffected —
`drfp_bitstring` standardizes every species itself before folding, which is what makes rows in the
two tables comparable at all.

### An unfingerprintable reaction is counted, never fatal

The same asymmetry `CorpusMolecules.add_many` documents for structures. A bulk extract's fiftieth
row may be a degenerate transformation with no extracted features; refusing the page over it loses
every good precedent beside it.

**What reaches that counter is narrower than it first reads, and it was measured rather than
assumed.** An empty fingerprint is the only case: `standard_smiles` returns a species RDKit cannot
parse *unchanged* (`"C(((C"` → `"C(((C"`), so `drfp_bitstring` shingles it and yields bits. The
reaction tier therefore has no unparseable-structure failure, while the molecule tier does
(`ecfp_bitstring("C(((C")` raises) — which is why `add_many` catches `InvalidSmilesError` and
`_fingerprint_reaction` deliberately does not. The first draft of this change caught both, which
would have been a guard for a case that cannot occur, and
`tests/test_reaction_corpus.py` pins the measurement so the narrowing is checkable rather than
believed.

Either way the reaction row is already written and still answers every facet query, so a skip costs
a similarity hit and never a wrong answer. `CorpusReport.unfingerprintable` is a **separate** counter
from `skipped` because the outcomes differ — a skipped row is not in the index at all — and
conflating them would report a corpus as less complete than it is.

## 2 — A daily fire re-walked the whole corpus

`corpus_sync.py` carried its keyset position inside one run and stored nothing. Its own docstring
gave the reason, and the reason was right for what it was written against:

> there is no `sync_cursors` row, because a re-drain of an unchanged release is a no-op (every write
> is an id-keyed upsert of the record phase) and a *new* release must be walked from the top

A vendor release is replaced wholesale, so there is nothing worth remembering. **A live feed is the
other case.** Every daily fire reads the entire corpus to discover the rows added since yesterday —
correct, because every write is an idempotent upsert, and O(whole corpus) per day forever. At the
scale a `corpus:` binding exists for, that is the difference between a delta and a full scan.

### Decision: the binding declares the property; the drain does not guess it

`CorpusBinding.append_only: bool = False`. It is worded as **a claim the binding author makes about
the source** — that `order_by` is monotonically increasing for new rows — because this side cannot
detect it and because the cost of it being wrong is a *silently skipped row*: a record inserted below
the watermark is never seen again. A release leaves it false and behaves exactly as it did before
this ADR, which is what makes the change additive in behaviour and not only in DDL.
`tests/test_corpus_cursor.py` asserts that default, since the whole safety of the change rests on it.

The position lives in `corpus_cursors (source, after, updated_at)` — **its own table, not a row in
`sync_cursors`**. That column is `TIMESTAMPTZ` and its contract is a datetime watermark; a keyset
position is a `TEXT` key in the source's own domain, which may be a bigint, a ULID or a padded
string. Widening `sync_cursors` would put two cursor kinds behind one name where a reader cannot tell
which it holds.

**Read and written in the activity, not the workflow**, for the reason every other IO in that file
is: a workflow replays deterministically and a database read cannot. The workflow already spells
"the start of this source" as an empty `after` — both on the first page and after it pops a finished
source — so that is the one moment a stored position is worth consulting, and no workflow change was
needed at all.

**Written every page, not only the last.** A run interrupted between pages must resume where it
stopped rather than where the previous *run* left off, and the write is one indexed upsert against a
page of thousands of rows. An empty position is never stored: a pass that advanced past nothing has
nothing to resume after, and writing it would overwrite a real position with a restart — which is
what would make the first quiet day of a live feed trigger a full re-walk.

**Deleting the row is the whole operator interface** for forcing a re-walk: after a binding change, a
`STANDARDIZATION_VERSION` bump, or a backfill the watermark would have hidden. That is also why the
table has no `release` column — naming the load a cursor belongs to would imply this side can detect
a new one, and it cannot.

## What this does not do

**No lag gauge, unlike `ingest/eln/cursor.py`.** That module can say how far behind a cursor is
because a datetime subtracts from `now()`. A keyset value is opaque — nothing here can tell how many
rows lie beyond it — so a gauge would have to invent a number, which is the failure
`D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose` is about. A feed that has stopped
advancing already shows up in the run's own `read`/`recorded`.

**`citation` is unchanged and was never the obstacle it was first read as.** `FieldBinding.path`
takes any dotted path, so a feed carrying only an id binds `citation: {path: root.REACTION_ID}`, and
`expr.TRANSFORMS` has `default` for the rest. Requiring one stays right: a precedent a chemist cannot
follow back is not a precedent.

**It does not touch the two larger open questions**, and both are in `docs/planning/BACKLOG.md` with
what would close them: a published calculation still names no reaction or note (so the result store
and the reaction tier cannot be joined), and structure identity is still canonical SMILES with no
InChIKey or external registry number. Neither is decidable against a result store that has never had
a live target, which is the row above both of them.

## Migration and rollback

`062_corpus_reactions.sql` and `063_corpus_cursors.sql` are `CREATE TABLE IF NOT EXISTS` and add no
column to an existing table, so the previous image runs unchanged against the migrated database —
`tests/test_migrations_are_additive.py` covers the shape. Rolling back leaves two unused tables and a
`corpus_reactions` index nothing writes; nothing reads them either, so there is no half-state.
`CorpusBinding.append_only` is `extra="forbid"`-safe in the additive direction only: a manifest that
sets it will fail to load on the previous image, which is the ordinary rule for a binding field and
is why the flag is opt-in.
