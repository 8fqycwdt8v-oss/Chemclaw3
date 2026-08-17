# Verification — `ingest/documents` correctness pass, reachability + consequence lens

Scope: the two findings marked **high**. The other five (medium/low) were not examined.

Working tree was clean at `01797786` before and after; no source file was mutated. Scripts in
`/tmp/v/`.

---

## One unlistable directory silently truncates the whole corpus at that point

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

Reproduced the reporter's shape independently (`/tmp/v/f1.py`): a mount holding `a_docs/one.md`,
`m_locked/hidden.md` (mode `0700`, root-owned) and `z_docs/two.md`, crawled in a forked child that
dropped to uid/gid 65534.

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

`z_docs/two.md` is in no list and in no counter, and `has_more` is `false`.

Traced the mechanism to the outermost entry point:

- `_Walk.descend` calls `os.scandir(directory)` (`crawl.py:196`) with no guard of its own; its
  docstring explicitly delegates the `OSError` to the caller.
- The only `try` is at root level (`crawl.py:270-276`), so the exception unwinds the entire
  recursion for that root. `entry.is_dir()` does **not** fail first — on Linux it answers from the
  `readdir` `d_type` without a `stat`, so the descent is entered and `scandir` is where `EACCES`
  lands. The repro confirms this.
- The `except` block does **not** `break`, so sibling roots are still walked. The finding's stated
  blast radius — "the rest of *this* root" — is precise, not exaggerated.
- Downstream: `sync_share` copies `failed_roots`/`has_more` straight into `SyncReport`
  (`sync.py:283-291`); `DocumentShareSyncWorkflow` sees `has_more=False`, calls `prune_document_share`
  (refused, correctly), pops the source and moves on (`document_sync.py:278-296`). Each run starts
  at `after=""` (`DocumentSyncState.after` default; the workflow docstring confirms there is no
  cross-run cursor), so the next scheduled run walks into the same wall.

### Why

Every element of the claim holds and I could find nothing upstream that prevents it.

**Reachability is high, not theoretical.** The shipped manifest
(`ingest/sources/sharedrive/datasource.yaml`) declares `roots: [Projects, SOPs]` with
`tag_from_path: {segment: 0}` — i.e. `Projects/<PROJECT_CODE>/...`. A per-project folder on a
Windows share that the mount credential cannot list is the ordinary case, not the exotic one; CIFS
maps server-side ACLs through, so one restricted project directory produces exactly this `EACCES`.
`ESTALE`/`EIO` on a flaky CIFS remount reaches the same line. Nothing between an operator's
manifest and this code narrows it: `RootBinding` validates path shape only, and `_is_excluded` would
skip such a folder only if someone already knew to exclude it.

**Consequence is as stated, with one imprecision.** The lost population moves no counter — I
confirmed `scanned`, `skipped_unsupported`, `skipped_oversized`, `unreadable` are all untouched for
`z_docs/two.md`, and `skipped_unreadable` is incremented only in `_parse_changed`, which never sees
it. Prune is correctly refused. But the finding's "it is invisible in the report" overstates by a
step: `failed_roots` **is** a `SyncReport` field, it survives `merge_reports`, the CLI prints it
(`cli/sync_share.py:124`), and `crawl_share` logs it at ERROR. What is invisible is the *magnitude*,
not the fact of a failure. I still rate this **high** rather than medium, because (a) `grep` shows
`failed_roots` has no metric and no alert anywhere in `src/` — in the scheduled deployment the only
artifact is a log line — and (b) the harm is the one this module's own docstring names: an arbitrary
tail of the corpus is absent, so a chemist asking about a project that sorts after the failing
folder is told the share holds nothing on it, permanently, with a clean-looking run.

**Note on the proposed fix.** The reporter's main fix (guard inside `descend`, record the directory,
`continue` to the next sibling) is right. The "independently" half — *"`crawl_share` should not
report `has_more: false` for a root it did not walk to completion"* — is wrong on its own: applied
alone it would make the workflow resume with `after=cursor`, re-walk into the same unlistable
directory, and loop the source forever. It is safe only *after* the `descend`-level guard exists.

---

## One crawl pass hands 50,000 texts to a single embedding request, and one failure stops the share forever

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

**1. Batch size, through the real code** (`/tmp/v/f2.py` — real `DocumentShareBinding` defaults,
real `chunk_document`, real `_chunks_for`, `embed_texts` counted):

```
chunk_chars = 1800  overlap = 200
500 x 50-page reports (shipped batch=500)
  embed_texts calls: 1  inputs per call: [50500]
  total chars sent : 54729890
```

And the same defect at entirely ordinary document sizes — the finding's 50-page report is not
needed:

```
  500 x 10-page docs -> 1 call(s), [10500] inputs
  500 x  4-page docs -> 1 call(s), [4500]  inputs
  500 x  2-page docs -> 1 call(s), [2500]  inputs
```

Anything averaging more than ~3 pages per file crosses 2048 inputs at the shipped
`document_sync_batch_size=500`.

**2. No batching on the provider seam** (`/tmp/v/f2b.py`, `_openai_client` stubbed):

```
embed_texts raised: RuntimeError 400 BadRequest: input array too large (50000 > 2048)
HTTP requests attempted: 1 inputs each: [50000]
```

One `client.embeddings.create(input=texts)`, whole list, no split — `core/embeddings.py:255`. The
in-process cache dedupes identical strings and does nothing else.

**3. The exception escapes `sync_share`** (same script, real `InMemoryDocumentIndex`, real crawl over
a temp mount):

```
sync_share raised: RuntimeError 400 BadRequest: too many inputs (3)
```

**4. The wedge, end to end against the live broker on :7233** (`/tmp/v/f2wf.py` — real
`DocumentShareSyncWorkflow`, real worker, `activity_max_attempts=2` to keep it short, a stand-in
provider that refuses any batch over 8 inputs):

```
run 1: WORKFLOW FAILED -> WorkflowFailureError: Workflow execution failed
        embed batch sizes attempted: [72, 72]
        index now holds 0 file row(s)
run 2: WORKFLOW FAILED -> WorkflowFailureError: Workflow execution failed
        embed batch sizes attempted: [72, 72]
        index now holds 0 file row(s)
```

Both retries build the identical 72-input batch; the second scheduled run reproduces it exactly and
lands zero rows. `RuntimeError` / `openai.BadRequestError` is not in `_BAD_DATA_TYPES`
(`durable/publish.py:32-42`), so `BAD_DATA_RETRY` retries it `activity_max_attempts` (default 5)
times and then fails the workflow.

### Why

Mechanism, trigger and consequence all hold, and two things make it worse than filed.

**Reachability.** No batching exists anywhere on the seam — not in `_chunks_for`, not in
`embed_texts`, not in `_embed_uncached`, not in `_openai_compatible_embeddings`. There is no
`embedding_batch_size` setting to be misconfigured. The only precondition beyond enabling the share
is `embedding_provider=openai_compatible`; the default is `hash`, which `core/embeddings.py`'s own
module docstring calls "explicitly the dev/CI path". A mounted CIFS share is a production feature,
so the production combination is the one that breaks.

**What I could not execute, and what replaces it.** I have no live OpenAI-compatible endpoint here,
so "the request is rejected outright" is inferred rather than measured. It is a safe inference —
50,500 inputs and a ~55 MB JSON body exceed the documented OpenAI cap by 25× and would meet a body-
size or token limit on any vLLM/TEI gateway too — but it is one step short of executed.

**A second leg the reporter missed makes the finding provider-independent.** One batch is unbounded
in *memory*, not just in request size. Measured (`/tmp/v/f2c.py`, `/tmp/v/f2d.py`) at the shipped
`embedding_dim=1536`:

```
2000 vectors: peak=99.9 MB   -> extrapolated to 50,500 vectors: 2523 MB
ChunkRecord construction for 2000 vectors added 26.9 MB (shared list? False)
                              -> extrapolated to 50,500: +680 MB
```

`embed_texts` holds every vector in `holding` before returning, and `ChunkRecord` validation copies
each one — ~3.2 GB resident for a single activity attempt. That happens under the **`hash`** provider
too, so even a deployment that never calls the endpoint OOMs the worker on a 500-file batch of real
documents. The `document_sync_batch_size` bound was written to keep an activity inside its window;
it does not bound the thing that actually grows.

**One consequence stronger than filed.** The activity failure propagates out of
`workflow.execute_activity` with nothing catching it, so the *workflow* dies at the first share in
`state.remaining` — every share later in the list is never crawled either, and `prune_document_share`
never runs for any of them. The finding scopes the wedge to "the same share"; it is the whole job.

`reembed_stale` does keep working (it has the per-chunk isolation guard the finding cites at
`sync.py:371-379`), so the failure is loud in Temporal but produces no wrong answer — it produces a
corpus that is never built. That is why I keep this at high rather than critical.
