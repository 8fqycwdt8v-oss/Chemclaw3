# Verdicts — `ingest/documents/` design findings, reachability/consequence lens

Scope: the two findings marked **high**. The seven medium/low findings in the source file are out
of scope and were not judged.

Infrastructure: Docker/Postgres/Temporal already up (`infra-postgres-1` healthy), schema migrated —
the live-Postgres test below ran rather than skipped.

---

## `ExternalVectorDocumentIndex.prune_stale` is a copy of the base's, and the hook that deletes it already exists

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

- **What I did**

  Read both methods (`external_index.py:202-234`, `index.py:873-887`) and the shared predicate at
  `index.py:566-569`. Then checked the finding's two load-bearing evidentiary claims by running the
  suite.

  1. Is the second copy untested? The finding says "`prune_stale` appears in the suite only against
     `PostgresDocumentIndex`, `tests/test_document_share.py:1654`". Line 1654 sits inside
     `test_the_external_store_backend_carries_the_chunking_through_every_write`, whose subject is
     bound at line 1578:

     ```
     $ sed -n '1578p;1654,1656p' tests/test_document_share.py
             index = ExternalVectorDocumentIndex(store, collection="chunks")
             assert await index.prune_stale("sharedrive-2", await index.clock()) == 1
             assert await _stored_cuttings() == [], "the unclaimed cutting was swept here, not later"
             assert await store.search("chunks", coarse_vector, 10) == [], "and its point went with it"
     ```

     ```
     $ uv run pytest "tests/test_document_share.py::test_the_external_store_backend_carries_the_chunking_through_every_write" -q -rs
     1 passed in 0.90s
     ```

     So the cited line is a test of *the subclass's* `prune_stale`, against live Postgres and the
     reference `VectorStore` — the exact opposite of what the finding says it is.

  2. Is that test load-bearing, or does it pass either way? Mutated the subclass's store-delete to a
     no-op (`if False and orphaned:`), re-ran, restored the file from a backup copy:

     ```
     E         Left contains one more item: VectorMatch(id='doc-1@4000:400#0', score=1.0)
     tests/test_document_share.py:1656: AssertionError
     1 failed in 1.25s
     $ git diff --stat -- src/chemclaw/ingest/documents/external_index.py   # (empty — restored)
     ```

     The override's distinguishing behaviour is mutation-covered.

  3. Is the orphan *rule* really spelled twice? No. Both statements interpolate the same module
     constant:

     ```
     index.py:566        CLAIMED_SQL = ("EXISTS (SELECT 1 FROM document_files f "
                                        "WHERE f.doc_id = c.doc_id AND f.chunking_key = c.chunking_key)")
     index.py:885        await cur.execute(f"DELETE FROM document_chunks c WHERE NOT {CLAIMED_SQL}")
     external_index.py:225   f"DELETE FROM document_chunks c WHERE NOT {CLAIMED_SQL} "
                             "RETURNING c.doc_id, c.chunking_key, c.ordinal"
     ```

- **Why**

  The mechanism is real: the subclass does restate the base's transaction scaffolding (two DELETEs,
  the `rowcount` read, the commit, the return) to add a `RETURNING` and a store delete, and the
  proposed merge onto the existing `_forget_vectors` hook is a genuine simplification. That much
  survives.

  What does not survive is everything that made it **high**:

  - *"The one rule the module insists must not be spelled twice — what an orphan is — is spelled
    twice anyway."* False. "What an orphan is" is one public constant, `CLAIMED_SQL`, imported by
    the subclass and interpolated by both. The historical divergence the subclass's own comment
    records ("when this said only `f.doc_id = c.doc_id`") is precisely the divergence that
    extracting `CLAIMED_SQL` closed — the finding cites the cure as evidence of the disease. What is
    duplicated is transaction *scaffolding*, not a predicate; a sixth clause added to the orphan
    rule lands in both statements automatically.
  - *"The second copy has no test of its own."* False, and demonstrably so: it has the only
    live-Postgres test in the slice that asserts both halves (catalogue row swept, vector point
    gone), and that test fails when the override is broken.

  So the trigger — "someone edits the orphan rule and the two copies diverge" — is not reachable
  through the predicate at all, and the residual trigger (someone edits the transaction shape) is
  caught by a mutation-sensitive test. There is no runtime defect, no reachable wrong answer, and
  no unguarded divergence path. That is a low-severity tidiness item: worth doing, not worth a high.

  One thing in the finding's favour that it did not claim: `ExternalVectorDocumentIndex` is
  genuinely reachable from operator config — `default_document_index()` (`index.py:1000-1005`)
  selects it whenever `vector_store_provider != "pgvector"` — so this is shipped code, not a stub.
  That does not change the verdict, because the code it ships is tested and correct today.

---

## A bounded crawl pass re-walks everything behind the cursor, so a drain is O(passes × share)

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  Traced the trigger from the outermost entry points inward, then measured it on a synthetic share.

  *Reachability.* Two real callers drain in a loop, both passing the previous pass's cursor back in:

  - `durable/document_sync.py:255-277` — `DocumentShareSyncWorkflow` calls `sync_document_share(source, state.after)`, sets
    `state.after = chunk.cursor` while `chunk.has_more`, and `continue_as_new`s every
    `document_sync_max_iterations` (default 100) *carrying the same cursor*. Nothing caps the pass
    count.
  - `cli/sync_share.py:113-120` — the same loop by hand.

  The pass bound is `document_sync_batch_size`, default **500** (`core/config/sources.py:72`), and
  the job is scheduled every `document_sync_schedule_minutes` = **360** (6 h,
  `durable/schedules.py:125-127`). So K = ceil(candidates / 500) passes per drain, on a timer,
  for any share this module was written for. Nothing upstream — no validator, no manifest field, no
  Helm default — bounds K.

  *Measurement* (`/tmp/cwv/drain.py`, 200 dirs × 100 `.txt` on tmpfs, `os.scandir` counted by
  wrapping it, limit = the shipped default 500):

  ```
  single pass      files=20000 entries=20200 scandirs=201 0.338s
  baseline         limit=  500 passes=  40 files= 20000 entries_examined=   421900 scandirs=  4179 3.397s
  with-subtree-skip limit=  500 passes=  40 files= 20000 entries_examined=    35800 scandirs=   318 0.361s
  identical output: True
  ```

  421,900 / 20,200 = **20.9 full walks of the share to index it once in 40 passes** — i.e. (K+1)/2,
  exactly the arithmetic of "pass k re-walks everything up to cursor k". 4,179 `scandir` calls where
  201 suffice; on CIFS each of those is a network round trip. The final pass examines all 20,200
  entries to return its last 500.

  *Soundness of the proposed fix* (`/tmp/cwv/diff.py`): an adversarial corpus deliberately seeded
  with the ordering traps `_order`'s docstring names (`Report` dir beside `Report.txt`, `Data`
  beside `Data-Archive`, three levels deep, 121 candidates), drained at four different limits with
  and without the patch:

  ```
  limit=1 passes 121/121 files 121/121 identical=True
  limit=2 passes 61/61   files 121/121 identical=True
  limit=3 passes 41/41   files 121/121 identical=True
  limit=7 passes 18/18   files 121/121 identical=True
  ```

- **Why**

  The mechanism is exactly as described — `descend` (`crawl.py:207-210`) recurses into every
  directory unconditionally and only tests the cursor per *file* at `:212` — the trigger is produced
  by both production callers on their ordinary path with shipped defaults, and the consequence is
  what is claimed. Nothing stands in the way.

  Two corrections, one in each direction:

  - *Down.* The headline "500 full walks" is the wrong arithmetic in general: a pass returns early
    once the chunk fills, so a K-pass drain costs ≈ **(K+1)/2** full walks, not K. (Measured: 20.9 at
    K=40.) It happens to land near the right number for a 500k-candidate share only because the
    finding assumed 1000/pass while the shipped default is 500, so K is 1000 and the answer is ~500
    anyway. The asymptotic claim — O(passes × share) — is correct either way.
  - *Up, and this is the part the finding missed.* This is not a first-crawl cost. `_accept`
    (`crawl.py:133-167`) admits every candidate that passes the extension and size filters; the
    fingerprint diff happens later, in `sync_share` (`sync.py:296-298`). So an **unchanged** share
    fills each 500-file chunk just as fast, and a steady-state scheduled run pays the same (K+1)/2
    walks — every six hours, forever. That makes `sync.py`'s own cost model, stated in its module
    docstring, false as written:

    > "A scheduled run over an unchanged share therefore costs one `scandir` pass and zero embedding
    > calls." (`sync.py:10-12`)

    It costs one `scandir` pass only for shares under 500 candidates. Above that it is ~K/2 passes,
    and on an unchanged share the crawl is nearly the whole cost of the run (the rest of the pass is
    one `fingerprints` SELECT and one `touch` UPDATE), so there is nothing to dilute it.

  Severity: no wrong answer, no data loss, nothing a chemist is shown — this is cost, which normally
  argues for medium. It clears high on three counts: it is unbounded in the share size this module
  exists for, it is permanent rather than one-off, and each pass is bounded by a 900 s
  `start_to_close_timeout` (`document_sync_timeout_seconds`) with `BAD_DATA_RETRY`'s
  `maximum_attempts` — so on a share large and slow enough that a late pass's re-walk alone exceeds
  15 minutes, the drain does not merely get slow, it fails the activity and the sweep never runs.
  I could not measure that threshold here (it depends on CIFS directory-listing latency I have no
  mount for), so I state it as the risk that makes the severity rather than as a measured fact; the
  20.9× and the 4,179-vs-201 round trips stand on their own.

  The fix is four lines, output-identical across every limit I could drive it at, and cut the walk
  work by 11.8× on the measurement above.
