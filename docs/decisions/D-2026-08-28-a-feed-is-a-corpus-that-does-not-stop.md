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

`corpus_reactions.id` is `<source>:<reaction_id>`, the pair rather than the bare id, so a hit can be
narrowed back to its `reaction_labels` row.

The table carries the same five columns as `003`, so `PostgresFingerprintStore` **ranks** over it
with no new SQL — the property `corpus_molecules` was built for. `tests/test_reaction_corpus.py`
drives that against the real database rather than the in-memory double, because the claim is about
a migration and an HNSW index and a doubled store cannot exercise either.

### But ranking is not a reader, and the first draft of this change shipped without one

Written that way, `corpus_reactions` was **write-only**: the drain filled it and nothing in `src/`
read it. Every reaction-similarity path binds `default_reaction_store()` → `reaction_fingerprints`,
and correctly so — this ADR's own decision above is that corpus rows must not go there. So the
defect the change opens with ("`similar_reactions` could not see a single row of it") would have
survived it, and what shipped would have been the shape this tree deletes on sight: a store whose
only evidence of working is that something writes to it.

The reader is `conditions_for_similar_reaction`, and it is a *precedent* question rather than a
second `similar_reactions` — the same place the molecule half is reached from, for the same reason.
`conditions_for_similar_products` finds neighbours in ECFP space and then looks up their recorded
conditions; this finds them in DRFP space and does the same. It is the question product similarity
cannot answer: a Buchwald and a Suzuki that make the same biaryl are neighbours by product and are
not the same reaction.

**The join is what makes that work, and it had to be built rather than asserted.**
`Facet.reaction_keys` narrows on `(r.source || ':' || r.reaction_id)` — composed in SQL so the
spelling matched is `corpus_reaction_id`'s own and the two cannot drift. It also belongs in
`_in_scope`, the coverage *denominator*, because unlike `product_smiles` it reads the reaction's own
key and an unlabelled row has one; leaving it out reported `total=10` against the in-memory
backend's `3`. That duplication — `_in_scope` and `_scope_coverage` being one condition written
twice — is now named in `_scope_coverage`'s docstring with the measurement that exposed it.

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

### The page is one write, not one write per row

The first draft wrote each fingerprint as it was built and justified it as making an interrupted
page "resumable at the row rather than at the page". That is false: the cursor only advances when
`drain_corpus` returns, so a retried activity re-reads the page from its start either way.

Measured against the live database inside `db.pooling()`, 200 rows, three trials: **3.0 ms/row**
one at a time against **1.15 ms/row** batched — 0.583/0.621/0.673 s versus 0.230/0.233/0.239 s, a
stable **2.6x**. `FingerprintStore.add_many` is the missing half of that interface
(`CorpusMolecules.add_many` has been the same method for the same reason one table over), and `add`
is now its single-record case so the upsert has one definition.

**A larger figure was reported to this branch and is not reproducible here**, which is why the
numbers above are the ones written down: a review measured 1.07 s against 0.09 s for the same 200
rows (~12x) and put the saving at ~19 h over a 13M-row corpus. Re-run three times on this database
the ratio is 2.6x, and the honest consequence is smaller than either figure suggests — batched, 13M
rows is still ~4 h of writes rather than ~11. The commit is not the whole cost, and a reader who
took this change for a bulk-load fix would be surprised.

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
the watermark is never seen again. A release leaves it false and is walked from the top exactly as it
was before this ADR. `tests/test_corpus_cursor.py` asserts that default, since the whole safety of
the change rests on it.

**`append_only` decides the cursor and nothing else** — worth stating, because an earlier draft of
this section said a release "behaves exactly as it did before", and that is not true of the other
half: `drain_reaction_corpus` passes the reaction index unconditionally, so an existing release
corpus starts writing `corpus_reactions` on its next drain. That is the intent — a vendor corpus is
exactly what one wants searchable by transformation — and with the write batched per page (below)
its cost is one connection and one commit per page. The test asserts both modes fingerprint.

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
