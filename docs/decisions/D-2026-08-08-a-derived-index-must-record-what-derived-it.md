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

**`embedding_config_key()` names the endpoint** for `openai_compatible` (`rstrip("/")`, because
`.../v1` and `.../v1/` address the same endpoint and a corpus-wide re-embed is too expensive to
trigger on a spelling). The slot stays, empty, for `hash` — that embedder never reaches the endpoint,
so naming it there would churn every dev vector on a setting that provably cannot change one.

**Migration 040** adds `chunking_key` to `document_files` *and* `document_chunks`, from one
definition (`DocumentShareBinding.chunking_key`). Two columns because there are two gates and a
change must be visible at both: the file row decides whether the document is re-read and re-cut at
all, the chunk row decides whether its text still needs embedding. Busting only the first re-parses
every file and then skips the chunking, because the content hash is unchanged and the embedding key
still matches. `upsert` also **deletes every ordinal at or above the new chunk count** per document,
in the same transaction, so a coarser re-chunk leaves no tail.

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

### The tie-break sorts the k rows, not the table

The deterministic `(-score, note_id)` ordering is kept — it is what makes the two backends agree —
but as an **outer** `ORDER BY` over the k rows the inner query already returned. Measured at
N=20,000, median of 5 EXPLAIN ANALYZE runs: shipped 243.05 ms (Seq Scan) → 3.58 ms (Index Scan +
a 10-row quicksort), and the ids are identical to the no-tie-break form in the same order.

### The cursor advances on the timestamp the entry was fetched by

`sync_entries` takes `entry_window(created_at, modified_at)` — the one existing definition of "the
timestamp an entry is filtered on", which every adapter already uses and the cursor was the single
place that did not. The future-timestamp guard is moved to the same value in the same change,
because a guard that checks a different timestamp from the one the cursor takes is not a guard.

### A retrieval leg yields no evidence, whatever happens

Both share retrievers get an `except Exception` backstop that logs with a traceback and returns
`[]`, and the warehouse one offloads `embed_texts` to a thread. Enumerating a vendor's exception
tree at these call sites would mean importing it; the contract is the promise already written in
both docstrings, so it is written as one.

## Consequences

- **A full re-embed on the first sync after upgrade, for both corpora.** Every `note_index` row has
  no key recorded (NULL reads as unknown, and unknown is never current), and every
  `document_chunks.embedding_key` changes because the key now names the endpoint. `reembed_stale`
  does the document half from stored text without touching the share; `reindex_notes` does the note
  half. Migration 040 is heavier still — the first crawl re-reads and re-cuts every file once,
  because what chunking the existing rows were cut with is not recorded anywhere and cannot be
  inferred. All three happen once, incrementally, under the existing bounded passes.
- **Dense note search is approximate again**, because it is now actually using the ANN index it was
  built with. Measured honestly: recall@10 against an exact scan is **1.0000** on clustered vectors
  (25 queries, N=20,000 — the shape a real corpus has) and **0.116** on uniformly random ones, which
  is the pathological case for any ANN index and not a corpus anyone has. `hnsw.ef_search` is not
  yet a setting; the BACKLOG row says when to add one.
- **Five protocol methods change signature**: `NoteIndex.upsert/fingerprints` take the embedding
  key, `DocumentIndex.fingerprints/known_documents/upsert` take the chunking key. Both protocols
  have two implementations, one caller each, and no third-party callers.
- `tests/warehouse_fake.py` gains `WatermarkWarehouse`, which honours WHERE/ORDER BY/LIMIT. Without
  it a wedged sync and a sync with nothing to do are indistinguishable, which is what the existing
  fake showed every test that ever looked.
- `PostgresDocumentIndex` gets its first test at all — the durable backend's statements had only
  ever run in production.
- Three prose claims that asserted properties the code did not have are corrected rather than
  deleted: the HNSW acceleration comment, `_remove_worktree`'s "never masking a live exception", and
  the runbook's paragraph telling operators that a model change needs a manual `reindex-full`.

## Alternatives rejected

**Fold the chunking into `embedding_key` instead of a second column.** One key is tidier and wrong:
`reembed_stale` would then see a chunk-size change as embedding staleness, re-embed the *old*
boundaries from stored text and stamp them current — after which the crawl's own gate would skip the
re-chunk that was the actual remedy. The two staleness kinds have different remedies (one is
database-to-database, one needs the file), so they are different columns.

**Fold the chunking into `doc_id`.** It is the content hash, and the appealing part is that stale
chunks would be orphaned and swept for free. It cannot work: the file-fingerprint gate short-circuits
before anything is parsed, so no new `doc_id` is ever computed. It would also change every citation's
identity to fix a staleness bug.

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
