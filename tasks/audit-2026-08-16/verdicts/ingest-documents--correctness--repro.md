# Adversarial re-derivation — `ingest/documents/` correctness pass

Lens: *does it actually reproduce?* Scope: the two **high** findings. The file has no **critical**
findings; everything else is medium/low and out of scope.

Working tree was clean at `01797786` before and after; my scripts are in `/tmp/verif/`, nothing in
the repo was mutated. I did not read or run any of the reporter's `/tmp/repro/` scripts.

---

## One unlistable directory silently truncates the whole corpus at that point

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  I wrote my own repro from the source (`/tmp/verif/f1_eacces.py`): a mount holding `a_docs/one.md`,
  `m_locked/hidden.md` and `z_docs/two.md`, with `m_locked` left root-owned at mode 700, crawled by
  `crawl_share` in a forked child that drops to uid/gid 65534. Nothing is stubbed — the real
  `DocumentShareBinding(roots=[{"path": "."}])` and the real `crawl_share`.

  ```
  root '.' could not be walked; nothing will be pruned
  {
    "files": ["a_docs/one.md"],
    "has_more": false,
    "cursor": "a_docs/one.md",
    "failed_roots": ["."],
    "unreadable": [],
    "skipped_unsupported": {},
    "skipped_oversized": 0
  }
  ```

  `z_docs/two.md` is absent from `files` and from every counter, and `has_more` is `false`.

  Because a `setuid` child is itself scaffolding of a kind, I reproduced the same unwind a second
  way with **no privilege trick at all** (`/tmp/verif/f1_eloop.py`): a directory symlink loop
  (`m_loop/self -> m_loop`) under `follow_symlinks: true`, which makes `os.scandir` raise `ELOOP`
  about forty levels down.

  ```
  file count : 42
  has_more   : False
  failed_roots: ['.']
  z_docs/two.md indexed? False
  ```

  42 entries, 41 of them the same document at growing depths, and the root abandoned before
  `z_docs`. Two unrelated triggers, one behaviour.

  I then traced the consequence rather than assuming it, in
  `src/chemclaw/durable/document_sync.py`. `sync_share` copies `crawl.has_more` into the report
  verbatim (`sync.py:290`), so `DocumentShareSyncWorkflow.run` takes the `not chunk.has_more`
  branch: it merges the reports, calls `prune_document_share`, `state.remaining.pop(0)` and moves
  on — the source is treated as drained. The next scheduled run enters with `state is None` and
  `after=""` (the workflow's own docstring notes it keeps no row in `sync_cursors`), so it walks
  from the top and stops at the same directory. The loss is not merely per-run; it is stable.

- **Why**

  The cited code is real and current. `descend` calls `os.scandir` at `crawl.py:196` with no guard
  of its own and recurses at `crawl.py:208`; the only `try/except OSError` is at `crawl.py:270-276`,
  at the *root* level, so an `OSError` raised at any depth unwinds every remaining sibling in that
  root. That is exactly what both experiments show.

  The trigger is ordinary, not exotic: one ACL'd folder on an SMB share, an `ESTALE` on a CIFS
  remount, a symlink loop. The consequence is a corpus silently missing everything lexically after
  that folder, repeated identically forever, with `has_more: false` asserting the drain finished.
  Nothing surfaces at query time either — the retriever simply finds no rows, and the agent reports
  the share knows nothing about the project.

  Two refinements to the finding, neither of which changes the verdict:

  - *"it is invisible in the report"* is imprecise. `failed_roots` **does** reach `SyncReport`
    (`sync.py:288`) and is logged at ERROR. What is invisible is the lost *population*: no counter
    moves, and `failed_roots` names only the root (`"."`), never the subdirectory that failed — so
    an operator who sees the line has no way to learn which folder or how much of the corpus is
    gone. The reporter's own text concedes `failed_roots` blocks the sweep, so this reads as loose
    wording rather than a wrong claim.
  - Worse than stated: the magnitude is "the rest of this root **in lexical order**", so a failing
    folder named `Archive` or `Admin` costs nearly the whole share, while one named `Z_scratch`
    costs nothing. Which is a coin flip on folder naming, not on severity of the underlying fault.

  The reporter's proposed fix is also the right shape — the guard belongs around each `os.scandir`
  in `descend`, and `has_more` should not read `false` for a root that was abandoned.

---

## One crawl pass hands 50,000 texts to a single embedding request, and one failure stops the share forever

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  Two measurements of my own, both end-to-end through the real `sync_share` against a temp share
  and `InMemoryDocumentIndex` — only `embed_texts` is *wrapped* to count, nothing on the path is
  replaced.

  1. `/tmp/verif/f2_batch.py` — 500 documents of 50 pages each (~3,100 chars a page of ordinary
     prose), shipped defaults throughout:

     ```
     shipped defaults: batch_size=500 chunk_chars=1800 overlap=200
     documents: 500, mean bytes/doc: 157119 (~3142 chars/page)
     embed_texts calls: 1
     inputs per call  : [62500]
     chars per call   : [90827633]
     report: scanned=500 indexed=500 embedded_chunks=62500
     ```

     My number is **62,500 inputs / 90.8 MB in one call**, against the reporter's 50,000 / 49 MB.
     Same phenomenon, larger — the gap is only how dense a "page" each of us wrote.

  2. `/tmp/verif/f2_seam.py` — the same pass with `embedding_provider=openai_compatible` and
     `_openai_client` replaced by a fake that records `len(input)`, so the count is taken at the
     *provider seam* rather than at `embed_texts`:

     ```
     --- accepting endpoint ---
     provider requests: 1 inputs each: [60000]
     report: scanned=500 indexed=500 embedded_chunks=60000

     --- refusing endpoint (400 too many inputs) ---
     provider requests: 1 inputs each: [60000]
     sync_share RAISED: RuntimeError Error code: 400 - {'error': {'message': 'Too many inputs...
     index file rows after the failure: 0
     ```

     Exactly **one** `client.embeddings.create(input=...)` carrying all 60,000 texts, and on a
     refusal the exception leaves `sync_share` with **zero** rows written.

  I then checked the retry/wedge chain rather than taking it on the finding's word.
  `_BAD_DATA_TYPES` (`durable/publish.py:32-60`) lists no `RuntimeError` and no
  `BadRequestError` — the SDK's actual exception — so the activity is *retryable*: it burns
  `activity_max_attempts=5` (`core/config/temporal.py:47`), fails, propagates out of
  `execute_activity`, and fails the workflow run. Nothing was indexed, and the next run restarts
  at `after=""`.

  `grep -rn "batch" src/chemclaw/core/embeddings.py` finds no `embedding_batch_size` and no slicing
  anywhere on that seam.

- **Why**

  Every deterministic link reproduces and exceeds the reported magnitude. `_chunks_for`
  (`sync.py:228-258`) accumulates every chunk of every fresh document in the pass into one
  `pending` list and hands it to a single `embed_texts` (`sync.py:245`), which reaches a single
  `client.embeddings.create` at `core/embeddings.py:255`. The bound on that list is
  `document_sync_batch_size=500` **documents**, not chunks — and the reporter understated the
  ceiling: with `max_file_bytes` defaulting to 50 MB, one legal pass may carry 500 × 50 MB ≈ 25 GB
  of text and ~15 million chunks in one Python list, plus the returned float vectors, in one worker
  process. 60,000 is the *ordinary* case, not the bad one.

  The one link I could not execute is the rejection itself, since there is no internal
  OpenAI-compatible endpoint in this sandbox. I hold it against the finding only as far as it
  deserves: OpenAI's own documented per-request cap is 2,048 inputs, and a 90 MB single POST is past
  the body limit of essentially any reverse proxy or gateway, before any token budget is considered.
  A request of this shape succeeding would be the surprising outcome. Given a rejection, the
  permanent wedge is measured above, and the asymmetry the finding points at is real and visible in
  the file: `_parse_changed` has reject-and-continue, `reembed_stale` has explicit per-chunk
  isolation with a docstring (`sync.py:371-379`) naming this exact failure — *"that one chunk
  stopped all document indexing, for every share, permanently"* — and the crawl call site, the one
  that runs on every pass, has neither.

  Two citation nits, neither material: the single `create(input=texts)` is at
  `core/embeddings.py:246-256`, not `228-238` as the finding states (symbol correct, ~18 lines of
  drift); the justifying comment is `sync.py:231-233` rather than `230-233`. Everything else —
  `sync.py:228-258`, the call at `sync.py:326`, `sources.py:72`, `binding.py:147` — is exact.

  The finding's reading of the docstring is fair, and worth restating because it is the actual
  defect: *"the provider seam is a batch API"* is true, and *"the difference between one request and
  a thousand"* is a claim about a seam that **bounds** its batches. This one does not, so the choice
  is not between 1 request and 1,000 but between 1 impossible request and ~30 possible ones.
