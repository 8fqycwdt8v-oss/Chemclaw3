# `src/chemclaw/ingest/documents/` — correctness pass

Slice: `crawl.py`, `binding.py`, `parse.py`, `formats.py`, `chunk.py`, `index.py`,
`external_index.py`, `sync.py`, `retriever.py`. All read in full. Reproductions were run under
`uv run` against the installed venv; scripts are in `/tmp/repro/`.

---

## One unlistable directory silently truncates the whole corpus at that point

- **Severity**: high
- **Location**: `src/chemclaw/ingest/documents/crawl.py:270-276` (`crawl_share`'s
  `try: walk.descend(...) / except OSError`), with `descend`'s `os.scandir` at `crawl.py:196`
- **Trigger**: any single directory under a root that `os.scandir` cannot list — an `EACCES` from a
  permission change on one folder, an `ELOOP` from a symlink loop, an `ESTALE`/`EIO` on a CIFS
  remount. The `try` is at the *root* level, so an `OSError` raised anywhere in the recursive
  descent unwinds the entire root.
- **Consequence**: everything lexically **after** the failing directory in that root is never
  examined, and the pass still reports `has_more: false` — i.e. it asserts the drain finished.
  `DocumentShareSyncWorkflow` therefore stops draining this source and never resumes past the
  failure, so every document sorting after that folder is absent from the corpus. It repeats
  identically on every scheduled run, because the crawl keeps no cross-run cursor. No counter moves:
  `scanned`, `skipped_unsupported`, `skipped_oversized`, `skipped_unreadable` are all unchanged for
  the lost population. `failed_roots` does correctly block the sweep, so nothing is *deleted* — the
  loss is on the indexing side only, and it is invisible in the report.

  The module docstring's claim that "every other failure mode here degrades to 'index less'" is
  true only in the weakest sense: the magnitude is "the rest of this root", not "the folder that
  failed". The `unreadable` field's own comment ("A transient `EACCES` on a subtree must not empty
  that subtree from the index") describes a *file*-`stat` failure; the directory-`scandir` failure
  does not go through that path at all.

- **Evidence**: `/tmp/repro/r8.py` — a mount with `a_docs/one.md`, `m_locked/hidden.md` (root-owned,
  mode 700) and `z_docs/two.md`, crawled in a forked child that dropped to uid 65534:

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

  `z_docs/two.md` is neither indexed nor counted, and `has_more` is `false`. The same shape
  reproduces without any permission trick via a directory symlink loop (`/tmp/repro/r6.py`,
  `follow_symlinks: true`, `a/self -> a`): `os.scandir` eventually raises `ELOOP` and the root is
  abandoned with 41 duplicate paths for one document.

- **Fix**: move the guard into `descend` — wrap the `os.scandir` of *each* directory, and on
  `OSError` record the directory in `CrawlResult.failed_roots` (or a new `failed_dirs` list that
  feeds the same prune refusal) and `continue` to the next sibling instead of unwinding the root.
  The walk then keeps its total order, the rest of the root is indexed, and the sweep is still
  refused because the failure is still reported. Independently: `crawl_share` should not report
  `has_more: false` for a root it did not walk to completion.

---

## One crawl pass hands 50,000 texts to a single embedding request, and one failure stops the share forever

- **Severity**: high
- **Location**: `src/chemclaw/ingest/documents/sync.py:228-258` (`_chunks_for`), called at
  `sync.py:326`
- **Trigger**: the shipped defaults. `document_sync_batch_size = 500`
  (`core/config/sources.py:72`) files per activity, `chunk_chars = 1800`
  (`binding.py:147`), and `chemclaw.core.embeddings.embed_texts` →
  `_openai_compatible_embeddings` issues exactly one `client.embeddings.create(input=texts)` with
  the whole list (`core/embeddings.py:228-238`) — there is no batching anywhere on that seam.
- **Consequence**: two things.

  1. A pass over 500 ordinary 50-page reports sends **one** HTTP request carrying **50,000** inputs
     and ~49 MB of text. OpenAI-compatible embedding endpoints cap inputs per request (OpenAI's own
     documented limit is 2048), so under `embedding_provider=openai_compatible` — the target stack —
     this request is rejected outright.
  2. When it is rejected, the exception propagates out of `asyncio.to_thread` → out of `sync_share`
     → the activity fails. `BAD_DATA_RETRY` bounds the attempts, the workflow fails, and the next
     scheduled run restarts from the top of the same share and reaches the same batch. **Nothing is
     ever indexed.** There is no reject-and-continue here: the parse path has one
     (`_parse_changed`), and the sibling call site of `embed_texts` has an explicit per-chunk
     isolation guard whose docstring (`sync.py:371-379`) names precisely this failure — *"that one
     chunk stopped all document indexing, for every share, permanently"*. That argument was applied
     to `reembed_stale` and not to the crawl, which is the call site that runs on every pass.

  The comment at `sync.py:230-233` justifies the single call as "the difference between one request
  and a thousand". That is a claim about a seam that batches, and this one does not.

- **Evidence**: `/tmp/repro/r10.py` — 500 synthetic 50-page documents through the real
  `_chunks_for` with `embed_texts` counted:

  ```
  500 x 50-page documents (defaults chunk_chars=1800, batch=500)
  embed_texts calls: 1  inputs in the single call: [50000]
  total chars sent  : 49275000
  ```

  And `core/embeddings.py:228-238` confirms the whole list goes in one `create(input=texts)`.

- **Fix**: chunk the embedding call — either a `embedding_batch_size` setting honoured inside
  `embed_texts` (which fixes every caller, `reindex_notes` included), or a loop in `_chunks_for`
  over slices of `pending`. Then give this call site the same isolation `reembed_stale` has: on a
  batch failure, retry the slice per document, count what could not be embedded into a report field,
  and let the rest of the pass land.

---

## `follow_symlinks: true` cannot read a single symlinked document

- **Severity**: medium
- **Location**: `src/chemclaw/ingest/documents/sync.py:153` (`_read_and_parse`'s
  `os.open(..., os.O_RDONLY | os.O_NOFOLLOW)`), against `binding.py:152` (`follow_symlinks`)
- **Trigger**: a binding with `follow_symlinks: true` (a supported option, documented with a
  rationale for its `false` default) over a share whose documents are reached through symlinks.
- **Consequence**: the crawl accepts them — `descend` skips the symlink only when
  `follow_symlinks` is false (`crawl.py:202`), and `entry.stat(follow_symlinks=True)` succeeds — and
  then the reader refuses every one with `ELOOP`, because `O_NOFOLLOW` is unconditional and does not
  consult the binding. Each such file is counted `skipped_unreadable`, gets **no index row** (by
  the deliberate design at `sync.py:205-211`), and is therefore re-opened and re-refused on every
  run forever. The configuration option is inoperative for files, and the documents it was enabled
  to reach are permanently absent.
- **Evidence**: `/tmp/repro/r2.py`:

  ```
  accepted   : ['link.md', 'plain.md', 'real/report.md']
  skipping link.md: [Errno 40] Too many levels of symbolic links: '/tmp/share-.../link.md'
  REPORT: {'scanned': 3, 'indexed': 2, ..., 'skipped_unreadable': 1, ...}
  indexed paths: ['plain.md', 'real/report.md']
  RUN2 unchanged=2 skipped_unreadable=1        # re-read and re-refused on the next run
  ```

- **Fix**: make the flag decide the flag —
  `os.open(ref.absolute, os.O_RDONLY | (0 if follow_symlinks else os.O_NOFOLLOW))`, threading
  `binding.follow_symlinks` onto `FileRef` or into `_read_and_parse`'s signature beside
  `max_bytes`. The TOCTOU argument in the docstring still holds for the `false` case, which is the
  default and the one that matters; with the flag on, the `fstat` size re-check remains.

---

## A trailing slash on a root path shifts every derived tag by one character

- **Severity**: medium
- **Location**: `src/chemclaw/ingest/documents/crawl.py:154`
  (`below = relative[len(root.path) + 1 :]`), enabled by `binding.py:86`
  (`RootBinding.path: str = Field(min_length=1)` — no normalization)
- **Trigger**: `roots: [{path: "Projects/", tag_from_path: {segment: 0}}]` in a hand-authored
  `datasource.yaml`. A trailing slash is accepted by every validator: `_stays_inside_the_mount`
  only rejects absolute and `..` paths, the duplicate/nesting checks compare raw strings, and
  `Path.__truediv__` normalizes the slash away when the directory is opened — so the *walk* works
  and only the arithmetic is wrong.
- **Consequence**: `relative` is `Projects/ACME-17/2024/report.md` (normalized), but `len(root.path)`
  is 9 not 8, so `below` starts one character late and `PathSegmentTag.extract` returns `cme-17`.
  The tag is well-formed, so `_TAG.match` passes and nothing is logged. Every document under that
  root is filed under a project code that does not exist; `DocumentFilter(tag="acme-17")` matches
  no file row, so a question scoped to that project returns zero evidence and the agent reports the
  share knows nothing about it.
- **Evidence**: `/tmp/repro/r9.py`:

  ```
  root='Projects'     -> [('Projects/ACME-17/2024/report.md', ('acme-17',))]
  root='Projects/'    -> [('Projects/ACME-17/2024/report.md', ('cme-17',))]
  ```

- **Fix**: normalize in the model — a `field_validator` on `RootBinding.path` that stores
  `PurePosixPath(path).as_posix()` (which also makes the duplicate and nesting checks compare
  normalized forms). Better still, stop doing string arithmetic: compute
  `below = PurePosixPath(relative).relative_to(root.path).as_posix()`, which cannot be off by one.

---

## Files the walk saw but could not `stat` are dropped from every report counter

- **Severity**: medium
- **Location**: `CrawlResult.unreadable` at `crawl.py:72`, populated at `crawl.py:147`, consumed
  only at `sync.py:304` (`index.touch(source, unchanged + crawl.unreadable)`). `SyncReport`
  (`sync.py:84-114`) has no field for it, and `merge_reports` (`sync.py:485`) cannot carry one.
- **Trigger**: any file whose directory entry lists but whose `stat` fails — an `EACCES` on the file
  itself, a dangling symlink under `follow_symlinks: true`, a race with a delete.
- **Consequence**: the file is not indexed and not counted anywhere. It is absent from `scanned`
  (never entered `crawl.files`), from `skipped_unsupported` (the extension passed), from
  `skipped_oversized` (never got a size) and from `skipped_unreadable` (that counter is incremented
  only in `_parse_changed`, which never sees it). The operator's report shows a clean pass. This
  contradicts the module docstring's stated property — *"And nothing is skipped silently … Silence
  would be read as 'the share held nothing else', which is the one answer that is never true"* — for
  exactly the population `SyncReport` was built to make visible. The restamping half is correct and
  should stay; the accounting half is missing.
- **Evidence**: `/tmp/repro/r2.py` — `dangling.md` appears in `CrawlResult.unreadable` and in no
  field of the returned `SyncReport`:

  ```
  unreadable : ['dangling.md']
  REPORT: {'scanned': 3, 'indexed': 2, 'unchanged': 0, ..., 'skipped_unreadable': 1, ...}
  ```

  (the `1` there is `link.md` from the parse phase; `dangling.md` is nowhere). Grep confirms
  `crawl.unreadable` has exactly one reader in the tree, the `touch` at `sync.py:304`.
- **Fix**: add `skipped_unstattable: int` (or fold into `skipped_unreadable` with a separate name,
  since the two are different populations) to `SyncReport`, set it from `len(crawl.unreadable)` in
  `sync_share`, and sum it in `merge_reports` — the same treatment `skipped_oversized` already gets.

---

## `.tsv` files are re-guessed as comma-delimited and rendered with the wrong columns

- **Severity**: medium
- **Location**: `src/chemclaw/ingest/documents/parse.py:100-119` (`_parse_csv`), specifically the
  `csv.Sniffer().sniff(dialect_sample, delimiters=",;\t|")` at line 110
- **Trigger**: a file whose content type is already *declared* as
  `text/tab-separated-values` (`formats.py:23`) but whose first 4096 characters contain a comma on
  every line — e.g. a header cell naming two things. The parser ignores the declared type and
  asks `Sniffer`, which prefers `,` among equally-consistent candidates.
- **Consequence**: the whole table is split on the wrong character, so cells are fused and the
  rendered column headers no longer line up with the values under them. What the agent is then
  handed as evidence is a table whose structure is wrong — the exact failure the function's own
  docstring says it exists to prevent: *"a mangled quote in a raw paste can silently shift a whole
  column — a wrong number a chemist would have no way to spot."*
- **Evidence**: `/tmp/repro/r5.py`, input `sample\tsolvent, temp\nA1\tDCM, 25\nA2\tTHF, 40\nA3\tMeCN, 60\n`:

  ```
  TSV declared as: text/tab-separated-values rows: 3
  'sample\tsolvent | temp\n----------------------------------------\nA1\tDCM | 25\nA2\tTHF | 40\nA3\tMeCN | 60'
  ```

  Three columns became two, with the sample id and the solvent fused into one cell. The tab —
  the delimiter the file's own declared type names — survives untouched inside the "cell".
- **Fix**: pass the delimiter down. `parse_document` already resolved the content type before
  dispatching, so `_parse_csv` should take it: `text/tab-separated-values` →
  `csv.excel_tab`, and sniff only for `text/csv`. One line at `_PARSERS` (a `partial`, or a two-arg
  parser signature) and the guess is confined to the one format that genuinely needs it.

---

## The chunker adopts a coordinate from any paragraph that opens `[page N]`

- **Severity**: low
- **Location**: `src/chemclaw/ingest/documents/chunk.py:26` (`_LABEL`) and `_blocks` at
  `chunk.py:40-56`
- **Trigger**: a document whose *own text* contains a line `[page 12]` (or `[slide …]` /
  `[sheet …]`) at the start of a `\n\n`-delimited block. Reachable in any format the parser does
  not label — a `.md`, `.txt` or `.docx` carrying a bracketed page reference, or a text export of
  an OCR'd document.
- **Consequence**: the line is consumed as a structural coordinate and **stripped from the chunk
  body**, so the text is no longer searchable, and every following chunk of that document is cited
  as `<path> [page 12]` — a page number in a document that has no pages. The comment above the
  regex asserts the opposite property: *"A document cannot forge a coordinate it was never given."*
  Narrowing the vocabulary to three words reduced the surface; it did not close it.
- **Evidence**: `/tmp/repro/r5.py`, input
  `"Intro paragraph about the assay.\n\n[page 12]\nsee Smith et al. for the calibration.\n\nThe yield was 84 percent.\n"`:

  ```
  Chunk(ordinal=0, content='Intro paragraph about the assay.', coordinate='')
  Chunk(ordinal=1, content='see Smith et al. for the calibration.\n\nThe yield was 84 percent.', coordinate='page 12')
  ```

  The `[page 12]` line is gone from the content, and a Markdown file is now cited by page.
- **Fix**: a coordinate is a fact about the *parser*, not about the text, so it should not be
  recovered by re-parsing the text. Have `parse_document` return
  `list[tuple[str, str]]` (coordinate, body) alongside `text`, or emit a delimiter that cannot occur
  in extracted content (an ASCII control character) and match on that. Failing that, restrict
  `_LABEL` to content types that actually emit labels — `_blocks` currently has no idea which
  parser produced its input.

---

## Checked and clean

Recorded so the absence of a finding is not read as an absence of a check.

- **The crawl's total order.** The claim in `_Walk._order` and `crawl_share` that keying a
  directory as `name + "/"` makes sibling order agree with joined-path order **holds**: for any two
  siblings, neither key can be a prefix of the other (names contain no `/`, and a file and a
  directory cannot share a name), so key order implies path order. Root ordering
  (`sorted(roots, key=lambda r: r.path + "/")`) is sound for the same reason, given the validator's
  distinct/non-nested guarantee. Verified by case analysis on `Report`/`Report.txt`, `D`/`D0`,
  `Data`/`Data-Archive`. The resume cursor is therefore monotonic and no file is skipped by a
  bounded chunk.
- **The resume cursor's placement.** `_accept` sets `cursor` for every entry it *examines*
  (including ones the extension filter turns away) and does **not** set it for the entry that
  overflows the limit, so `after=cursor` resumes on exactly the next unexamined entry — no gap and
  no double count. The `has_more`/`limit=0` interaction is also safe: `limit=0` yields
  `files=[]` with `has_more=True`, and `prune_share` tests `has_more` before `scanned == 0`.
- **The prune-safety rule.** All three refusals in `prune_share` are correctly derived from the
  merged report; the workflow's `merge_reports` folding preserves `failed_roots` across
  `continue_as_new` compaction and takes `has_more` from the last chunk, which is the terminating
  one. `PostgresDocumentIndex.prune_stale` reads `cur.rowcount` before the second `DELETE`.
- **`chunk_document` loses no content.** 300-trial fuzz over random line lengths, chunk sizes
  {200, 400, 1800} and overlaps {0, 50, size-1}: every short line survives into the concatenated
  chunks (`/tmp/repro/r7.py`, "trials with a lost short line: 0"). The overlap tail arithmetic
  converges and cannot grow a piece without bound.
- **Score bounds across the three backends.** `_cosine` clamps, `_run` clamps, and
  `ExternalVectorDocumentIndex._resolve` does *not* — but it doesn't need to, because
  `VectorMatch.score` is itself `Field(ge=0.0, le=1.0)` at the seam
  (`retrieval/vectors/base.py`). No `ValidationError` path.
- **Citation resolution.** `_ELIGIBLE` and `CITATION_SQL` share `_FILE_MATCH`, so
  `min(f.path)` is non-NULL exactly when `EXISTS` is true; `_run`'s `if row[5]` filter is
  defensive, not load-bearing, and cannot silently shorten a result page in the pgvector backend.
- **`upsert` ordering and the unclaimed-cutting sweep.** Chunks before file rows before
  `_drop_unclaimed`, all in one transaction, with `_forget_vectors` after the commit; the in-memory
  reference mirrors the same order and the same `(doc_id, chunking_key)` claim predicate.
- **`_read_and_parse`'s descriptor handling.** The `descriptor = -1` handoff to `os.fdopen` is
  correct on every path — a failure in `fdopen` still closes, and a failure inside the `with` does
  not double-close.
- **`_is_excluded`'s leading-slash trick.** `fnmatch("/Archive/old.pdf", "**/Archive/**")` is
  `True` and `fnmatch("Archive/old.pdf", "**/Archive/**")` is `False`, as the docstring states. The
  parent *directory* entry is not matched by that pattern, so it is descended and each file inside
  is excluded individually — slower, same outcome.
