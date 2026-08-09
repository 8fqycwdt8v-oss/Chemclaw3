# D-2026-08-08-a-derived-index-must-record-what-derived-it — a derived index must record what derived it

**Status:** accepted

## Context

A campaign of adversarial reviews measured eight defects in the index and ingest paths against a
live Postgres 16 + pgvector. They are not one bug, but four of them are one *sentence*: **an index
derived from something else has to record what derived it, or it cannot tell whether it is stale.**

`D-2026-08-06-a-vector-is-only-good-for-the-model-that-made-it` established that rule and applied it
to `document_chunks`. This closes the cases it did not reach, plus four independent defects in the
same files.

**1. `note_index` recorded no embedding identity at all.** It keys on the file's `mtime_ns:size`
fingerprint (035), and changing the embedding model moves no file's mtime — so `reindex_notes`
computed `changed = []` and re-embedded nothing. Measured on live pgvector in a scratch schema:
`[A] embedded 3` → swap model → `[B] embedded 0`, every stored vector byte-identical (sha1 per row).
After one note was then edited, a model-B query scored the note whose text it matched **exactly** at
cosine 0.0000 and an unrelated note at 0.3333; `search_dense` floors at `> 0`, so the three
superseded notes were dropped outright and the production path returned **1 of 4 notes**. Nothing
failed. The documented workaround (`BACKLOG`, "run `make reindex`") named the *incremental* target
and measured zero; only `--full` healed it, and the hourly workflow never passes it.

**2. `embedding_config_key()` omitted the endpoint.** `provider:model:dim`, so two `Settings`
differing only in `llm_base_url` produced `openai_compatible:text-embedding-3-large:1536`,
`identical: True`. A model name is not globally unique — the vendor's and a gateway's need not be
the same weights — so repointing the deployment left every stored key reading as current, in
`document_chunks` as well.

**3. Chunking parameters were outside document identity.** `chunk_chars` decides what text each
vector describes and was recorded nowhere, so neither of the crawl's two gates could see it change:
2000 → 400 left the stored chunk sizes at `[1248, 1951, 1962, 1962]`. And when a re-chunk *did*
happen for another reason, `upsert` replaced ordinals `0..n-1` and left the finer cutting's tail
behind — 400 → 4000 gave `[229, …, 396, 3046, 3981]`: 2 real chunks and 19 orphans on one document,
which `reembed_stale` then re-embedded under the current key, making them indistinguishable.

**4. A note whose filename stem ≠ its id was never indexed at all.** `note_file_fingerprints` keys
on `path.stem` (stat-only — it never parses), `reindex_notes` looks up by the frontmatter id; on
disagreement both sides are `None`, `None != None` is False, so the note read as "unchanged"
forever and `full=True` took the same branch. Reproduced on both backends: `load_notes` parsed
`['ethanol-facts', 'good-note']`, the index held `['good-note']`. `kg-validate` did not check it.

Then four defects that share only these files:

**5. A pushed note could be recorded FAILED, or not at all.** `_submit_locked`'s `finally` called
`_remove_worktree`, which can raise *after* the branch is on the remote. With real git and real
`propose_note`: branch `note/crash-demo` on origin with the note's bytes, `durable proposal row:
state='failed'`, reviewer queue 0, `close_merged_notes` moved 0. With `CancelledError` — a
`BaseException`, so `except Exception` at the caller never saw it — the branch was pushed and there
was **no durable row of any kind**. The method's docstring claimed it never masked a live exception;
a raising `finally` both masks one and manufactures one.

**6. The `, note_id` tie-break disabled the HNSW index entirely.** EXPLAIN ANALYZE at N=20,000, one
clause changed at a time: the shipped query planned a Seq Scan + Sort at **243.05 ms**; the same
query without the tie-break used `note_index_embedding_idx` at **11.41 ms**. The class docstring
asserted the search was "accelerated by the HNSW index".

**7. Warehouse ELN sync wedged permanently.** `sql.entry_statement` filters, orders *and truncates*
on `COALESCE(modified, created)`; `sync_entries` advanced its cursor on `RawEntry.created_at` alone.
Once more than one page of already-created rows has been amended, every fetch returns that same page
forever and reactions created afterwards are never ingested. No guard fires: the batch is not
truncated by the workflow's reckoning, so the wedge guard at `durable/eln_sync.py` is never reached
and the log reads `ingested=500 rejected=0`. The repo's own `tests/warehouse_fake.py` ignores WHERE,
ORDER BY and LIMIT, which is why no test could see it.

**8. The warehouse retriever embedded on the event loop**, and let the provider's own exception type
escape. `retrieve()`: 1.00 s wall, 0 heartbeats where a free loop runs ~20; and a provider error
raised straight through into `gather_evidence`'s `gather` (no `return_exceptions`), failing the whole
turn including the answer the knowledge graph had already produced. Its sibling
`ingest/documents/retriever.py` offloads with a comment giving exactly this reason — and its own
"**Never raises**" docstring was untrue for the same provider-error case.

## Decision

### The identity of a derived row is stored beside it, and the gate compares it

**Migration 039** adds `note_index.embedding_key`; `PostgresNoteIndex.upsert` writes
`embedding_config_key()` into it and `fingerprints(embedding_key)` reports only rows made by the
current configuration. A superseded row therefore has no stored fingerprint to match and is
re-embedded by the ordinary incremental run — the same shape 038 gave `document_chunks`, so the
hourly workflow heals a model swap with no flag anybody has to remember at the moment they change a
setting.

**`embedding_config_key()` identifies the endpoint** for `openai_compatible` — a twelve-character
digest of `llm_base_url.rstrip("/")`, not the URL (`.../v1` and `.../v1/` address the same endpoint
and a corpus-wide re-embed is too expensive to trigger on a spelling). The slot stays, empty, for
`hash` — that embedder never reaches the endpoint, so naming it there would churn every dev vector
on a setting that provably cannot change one.

**A digest rather than the URL, and this too corrects a first answer.** The key is written into
`document_chunks.embedding_key` and `note_index.embedding_key`, one copy per row, in tables nothing
prunes and the runtime role can read. `llm_base_url` is a plain `str` with no validator forbidding
userinfo and is not in `logging._SECRET_SETTINGS`, so `https://svc:token@llm.internal/v1` is a
configuration this deployment accepts — and the verbatim form persisted the password:
`db: openai_compatible:https://svc:s3cr3t-token@llm.internal/v1:…`, while the log line for the same
value came out redacted by the structural `_URL_USERINFO` pattern. Even with no credential, an
internal hostname does not belong in every row of a corpus. The digest keeps the only property the
key needs and the property is asserted directly: two endpoints still produce different keys, and one
endpoint spelled two ways still produces one.

**Migration 040** adds `chunking_key` to `document_files` *and* `document_chunks`, from one
definition (`DocumentShareBinding.chunking_key`). Two columns because there are two gates and a
change must be visible at both: the file row decides whether the document is re-read and re-cut at
all, the chunk row decides whether its text still needs embedding. Busting only the first re-parses
every file and then skips the chunking, because the content hash is unchanged and the embedding key
still matches.

**Migration 041 puts the chunking in the chunk row's primary key, and this corrects 040's first
answer, which destroyed data.** 040 left the key at `(doc_id, ordinal)` and had `upsert` delete
every ordinal at or above the new chunk count per document. `doc_id` is the hash of the parsed text
and is shared across sources *by design* — the same report in four project folders is one set of
chunks, which is what makes a TB share affordable — while `chunking_key` comes from the per-share
binding. So two shares holding one document at different chunk sizes fought over the same rows: the
coarse share's write took ordinal 0 through `ON CONFLICT` and the tail-drop deleted ordinals 1..15
as though they were its own stranded tail. The victim never repaired, because its own file row's
`mtime_ns:size` had not moved and its gate read `unchanged` forever. Measured in the reference
index: the fine share then served **one chunk of 6259 characters in place of its own sixteen of at
most 400**, permanently.

A chunk row is derived from the content *and* the boundaries, so both are its identity. With the
chunking in the key, two cuttings coexist and neither can overwrite the other, while four copies at
one chunking still share one set of chunks and one embedding call. The tail-drop goes with it and is
not replaced by a narrower version: within a single `(doc_id, chunking_key)` the cutting is a pure
function of those two, so it can never produce fewer rows than last time and the delete could never
fire. What supersedes a cutting is that **no file row names it any more**, and that is one predicate
(`_CLAIMED`) used in two places — `upsert` applies it to the documents it just wrote, in the same
transaction and *after* the file rows so it reads what the write said, and `prune_stale` applies it
to the whole table. The search's eligibility predicate joins on the chunking too, so a share cites
its own cutting and never another share's.

`document_chunks.chunking_key` becomes `NOT NULL`, with pre-040 rows backfilled to `''` — a value no
binding can produce, so both gates still read those rows as superseded exactly as 040 promised,
while they stay *searchable* until the crawl replaces them rather than vanishing on upgrade.

**A note the fingerprint scan does not know is embedded, not compared.** `_needs_embedding` treats
an absent current fingerprint as "unknown", which means "embed it", and says so at WARNING naming
the file it expected. `kg.validate` refuses a note whose file is not `<id>.md`, so the mismatch fails
a PR instead of silently shrinking the index — but the indexer still handles one, because the tree a
pod serves is not the tree that passed a PR.

### Cleanup may never cost a submission its result

`_release_worktree` wraps `_remove_worktree` and swallows everything it raises, **including
`CancelledError`**. The obligation is genuinely one-sided: the branch is the product of the
submission and already exists, while an unremoved scratch tree costs disk under `.git/` until the
next submission's sweep reclaims it (which the regression test asserts, so the swallow is not a
leak). Cancellation is swallowed for the same reason — a caller that must record what was pushed
cannot be told the call did not finish. `_remove_worktree` itself still raises, which is right for
the pre-push sweep that also calls it, and its docstring now says which caller is which.

`BaseException` means every one of them, and two consequences follow that the first version of this
section left unsaid. An operator's **Ctrl-C landing inside this window is logged as a warning and
goes no further** — the process finishes recording the submission it was in the middle of and exits
on the next one. And a task **cancelled at that instant still runs `record_proposal_submitted`**:
one bounded database write, under the connection's statement timeout, itself swallowing failures.
Both are the intended price of a pushed branch never being recorded `failed`; neither makes shutdown
unbounded.

### The tie-break sorts the k rows, not the table

The deterministic `(-score, note_id)` ordering is kept — it is what makes the two backends agree —
but as an **outer** `ORDER BY` over the k rows the inner query already returned. Measured at
N=20,000, median of 5 EXPLAIN ANALYZE runs: shipped 243.05 ms (Seq Scan) → 3.58 ms (Index Scan +
a 10-row quicksort), and the ids are identical to the no-tie-break form in the same order.

**The commit message that carried this change overclaims, and cannot be rewritten, so the
correction lives here.** It reads "243.05 ms to 3.58 ms … with identical ids in identical order",
which plainly says the two forms return the same rows. They do not, and the qualification matters:
restoring the index restored *approximate* search, which the accidental Seq Scan had been hiding.
Independently re-measured, 235.94 ms `['Limit','Sort','Seq Scan']` → 3.40 ms
`['Sort','Subquery Scan','Limit','Index Scan']`, with recall@10 against an exact scan of **0.165 on
uniformly random vectors** and 0.975–0.994 on clustered ones. What the tie-break pins is that the
two backends agree on the order of the hits they *do* return — never which rows win a tie at the
k-th place, which the inner form did not pin either. The trade-off is characterised correctly in the
Consequences below; only the commit message dropped the qualifier.

**The same defect was left in `document_chunks.search_dense`, and is fixed here rather than filed.**
That table carries its own HNSW index and is the one designed to hold millions of rows from a
500k-file share, so the argument applies to it more strongly than to `note_index`'s thousands.
Measured on a synthetic 20,000-chunk corpus with the migrations applied, median of 5:
`Limit → Sort → Seq Scan` **228.25 ms** → `Sort → Limit → Index Scan` **2.47 ms**, identical ids in
identical order on that corpus — with the same qualification as above.

### The cursor advances on the timestamp the entry was fetched by

`sync_entries` takes `entry_window(created_at, modified_at)` — the one existing definition of "the
timestamp an entry is filtered on", which every adapter already uses and the cursor was the single
place that did not.

**The future-timestamp guard splits rather than moving wholesale, correcting a first answer that
silently dropped data.** Moving the guard onto `entry_window` gave it a second job nobody stated: an
entry created in 2026 and *amended* with a typo'd 2062 was rejected outright and, because the fetch
filters on the same watermark, re-fetched and re-rejected on every run, forever — a real experiment
lost to a typo in a metadata field. So the two halves are separated by what they mean. A **creation**
stamp past the wall clock says the record is not about anything that has happened: reject and report
it, as before. An **amendment** stamp past it costs only the cursor — the entry ingests, a WARNING
names it, and the cursor stays where the batch's plausible entries put it, which is exactly the
guard's stated purpose (nothing ever lowers a stored cursor, so an implausible value that became one
would skip every later real entry). The entry is re-fetched each run and, once its note is merged,
skipped by the body comparison at the cost of one lookup.

### A retrieval leg yields no evidence, whatever happens

Both share retrievers get an `except Exception` backstop that logs with a traceback and returns
`[]`, and the warehouse one offloads `embed_texts` to a thread. Enumerating a vendor's exception
tree at these call sites would mean importing it; the contract is the promise already written in
both docstrings, so it is written as one.

## Consequences

- **A full re-embed on the first sync after upgrade, for both corpora.** Every `note_index` row has
  no key recorded (NULL reads as unknown, and unknown is never current), and every
  `document_chunks.embedding_key` changes because the key now identifies the endpoint.
  `reembed_stale` does the document half from stored text without touching the share;
  `reindex_notes` does the note half. Migration 040 is heavier still — the first crawl re-reads and
  re-cuts every file once, because what chunking the existing rows were cut with is not recorded
  anywhere and cannot be inferred. Each happens once, incrementally, under the existing bounded
  passes.
- **That "once" was not true as first written, and the fix is in the drain rather than the prose.**
  `reembed_stale` runs *ahead* of the crawl, and on the first upgraded run both keys have moved at
  the same time — so it refreshed the whole old cutting from stored text and the crawl then
  re-parsed, re-cut and re-embedded the same text. Measured on the reference index: **17 embedding
  calls for a document worth 1**. It is not fixable by stamping the chunking during a re-embed,
  which the shape of the defect invites: the chunking is part of the row's identity (041) and a
  re-embed does not re-cut anything, so that would write a cutting the row does not have.
  `stale_chunks` is instead scoped to the chunkings the *enabled shares currently use* — a row cut
  under any other one is about to be replaced by the crawl, so refreshing it is work thrown away.
  With that, the upgrade costs one embedding per chunk, once.
- **A drain is a window in which the corpus is of mixed generation, and nothing hides that.**
  `reembed_stale` refreshes `document_reembed_batch_size` chunks per activity and the workflow loops
  `document_sync_max_iterations` times per run, so at 500 × 100 per six-hourly run a million-chunk
  share takes on the order of **days** to drain. Throughout it, `search_dense` filters on the
  chunking (through the file row) but **not** on the embedding key, so a query embedded by the new
  model is compared against not-yet-refreshed vectors made by the old one. That is deliberate and is
  the lesser of the two errors available: filtering on the embedding key instead would make the
  un-drained portion *invisible*, which is precisely the "returned 1 of 4 notes" failure this whole
  decision exists to prevent. A degraded score for part of a drain is recoverable; a silently
  shrunken corpus is the thing that was not noticed for months.
- **Dense note search is approximate again**, because it is now actually using the ANN index it was
  built with. Measured honestly: recall@10 against an exact scan is **1.0000** on clustered vectors
  (25 queries, N=20,000 — the shape a real corpus has) and **0.116** on uniformly random ones, which
  is the pathological case for any ANN index and not a corpus anyone has. `hnsw.ef_search` is not
  yet a setting; the BACKLOG row says when to add one.
- **Protocol methods change signature**: `NoteIndex.upsert/fingerprints` take the embedding key;
  `DocumentIndex.fingerprints/known_documents` take the chunking key, `stale_chunks` takes the live
  chunking set, and `upsert` takes it no longer — the chunking moved onto `FileRecord`,
  `ChunkRecord` and `StaleChunk`, because it is part of which row each one *is* rather than a
  property of the write. Both protocols have two implementations, one caller each (plus
  `cli/sync_share.py`), and no third-party callers.
- `tests/warehouse_fake.py` gains `WatermarkWarehouse`, which honours WHERE/ORDER BY/LIMIT. Without
  it a wedged sync and a sync with nothing to do are indistinguishable, which is what the existing
  fake showed every test that ever looked.
- `PostgresDocumentIndex` gets its first test at all — the durable backend's statements had only
  ever run in production.
- Prose claims that asserted properties the code did not have are corrected rather than deleted: the
  HNSW acceleration comment, `_remove_worktree`'s "never masking a live exception", the runbook's
  paragraph telling operators that a model change needs a manual `reindex-full` — and
  `vector_index.py`'s claim that `within` "bounds the search *before* the LIMIT". That last one
  became false the moment the HNSW index was restored: measured, the plan is
  `Index Scan using note_index_embedding_idx` with `Rows Removed by Filter` above it, so the
  eligibility set is a *post*-filter over the ef_search candidate list and the query can return
  fewer than k. At N=20,000, k=8, clustered vectors, the planner falls back to a Seq Scan once the
  filter is selective enough — but forcing the index at `within=0.10` returned **5 of 8**.
  `GraphRetriever` always passes a `within`, so this is the only path production takes, and a
  `type=` filter correlated with the embedding clusters will be worse than the random subset
  measured. No knob trades latency back for recall; the existing `hnsw.ef_search` BACKLOG row is
  where one would come from, and it is cross-referenced rather than duplicated.

## Alternatives rejected

**Fold the chunking into `embedding_key` instead of a second column.** One key is tidier and wrong:
`reembed_stale` would then see a chunk-size change as embedding staleness, re-embed the *old*
boundaries from stored text and stamp them current — after which the crawl's own gate would skip the
re-chunk that was the actual remedy. The two staleness kinds have different remedies (one is
database-to-database, one needs the file), so they are different columns.

**Fold the chunking into `doc_id`.** It is the content hash, and the appealing part is that stale
chunks would be orphaned and swept for free. It cannot work: the file-fingerprint gate short-circuits
before anything is parsed, so no new `doc_id` is ever computed. It would also change every citation's
identity to fix a staleness bug. Putting the chunking in the *primary key* alongside `doc_id` (041)
gets the orphaning without either cost.

**Scope the tail-drop to the chunking instead of changing the key.** The one-line version of the 041
fix, and it is insufficient rather than merely inelegant: `ON CONFLICT (doc_id, ordinal)` still lets
the coarse share's ordinal 0 overwrite the fine share's, so the corruption survives at reduced size
and the victim's gate still reads `unchanged` forever. The primary key is the defect.

**Keep the tail-drop in a narrowed form, for safety.** It would be dead code. Within one
`(doc_id, chunking_key)` the cutting is a deterministic function of the text and the two chunk
settings, so a re-write under the same key can never produce fewer rows than the last one, and the
delete could never fire. What it was written to catch — a re-chunk leaving the previous cutting
behind — is now a different chunk set, caught by the unclaimed-cutting sweep.

**Truncate the whole chunk row set for a document on re-chunk, and let each share re-add its own.**
Simple, and it makes two shares evict each other on every crawl instead of once: the property to
preserve is that a share's rows survive another share's write, and only per-chunking identity gives
it.

**Reject an entry whose amendment timestamp is in the future** (what the first form of the cursor
fix did). It is the harsher reading of a guard whose purpose is the cursor, and its cost is a real
experiment dropped for a typo in a metadata field — permanently, and re-litigated on every run,
because the fetch filters on the same watermark that made it implausible.

**Stamp `chunking_key` during `store_embeddings` to stop the double embed.** The one-line version of
the F3 fix, and it is wrong rather than merely sufficient: the chunking is part of the row's
identity, and a re-embed reads a chunk's stored text without re-cutting it, so stamping the current
chunking would claim boundaries the row does not have — and the search, which now joins on the
chunking, would then serve them.

**Make `note_file_fingerprints` key on the parsed note id.** That would make both sides of the diff
agree by construction — and it would have to read and parse every note file, which is exactly what
that function exists not to do (035 exists because the hourly rebuild was re-embedding a corpus that
had not changed). Treating "absent" as "unknown" costs one re-embed per malformed filename and
nothing at all in the steady state.

**Add a `--full` flag / document a manual step, for either staleness.** The remedy for a silent
defect cannot be gated on already knowing about it. This is the argument
`D-2026-08-06-a-vector-is-only-good-for-the-model-that-made-it` made, applied to the two tables it
did not reach.

**Drop the `note_id` tie-break rather than moving it.** It would have been the fastest of the three
forms and it removes the property the two backends are checked against each other for. Sorting ten
rows costs nothing.

**Re-raise `CancelledError` after cleanup in the submitter.** Formally the polite thing to do, and it
reintroduces exactly the defect: the caller must record a branch that exists, and an exception is not
a branch name. What is genuinely lost is that whoever cancelled gets the result of an operation that
had already succeeded — which is the right answer to "was it pushed?".
