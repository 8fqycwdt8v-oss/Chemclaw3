# `src/chemclaw/ingest/documents/` — security and hardening

Slice: `binding.py`, `crawl.py`, `formats.py`, `parse.py`, `chunk.py`, `index.py`,
`external_index.py`, `sync.py`, `retriever.py`. All read in full. Reproductions under `/tmp/aud/`,
run with `uv run` against the repo venv.

---

## `O_NOFOLLOW` guards only the last path component, so a parent-directory swap reads outside the mount

- **Severity**: high
- **Location**: `src/chemclaw/ingest/documents/sync.py:136-171` (`_read_and_parse`), specifically
  line 153 `os.open(ref.absolute, os.O_RDONLY | os.O_NOFOLLOW)`
- **Trigger**: the share is writable by its members — which the function's own docstring states as
  the threat model ("on a share every member can write to, deliberately so"). Between the crawl
  (`crawl_share`, run in a worker thread at `sync.py:282`) and the read (`sync.py:204`), a member
  replaces an **ancestor directory** of an accepted file with a symlink:

  ```
  crawl accepts  <mount>/Projects/report.txt
  member does    mv Projects Projects.real ; ln -s /var/run/secrets Projects
  worker opens   <mount>/Projects/report.txt        # O_NOFOLLOW is satisfied: report.txt is a file
  ```

  No race against the *file* is needed. `O_NOFOLLOW` refuses only a symlink as the **final**
  component; every directory in the path is still traversed through links.
- **Consequence**: arbitrary files readable by the worker's UID are parsed, chunked, embedded and
  stored in `document_chunks` under a `path` that still looks mount-relative, and are then returned
  as cited evidence by `ShareDocumentRetriever` to anyone holding the share's entitlement. The
  container filesystem is in scope — service-account tokens, `/proc/self/environ`-adjacent config,
  the knowledge repo, mounted secrets. `crawl.py:110-121` (`_within_mount`) exists precisely to stop
  a root symlink from doing this, and the same escape is reachable one level down at read time.
  This also contradicts the docstring at `sync.py:141-145`, which asserts `O_NOFOLLOW` "refuses a
  path that became a symlink — pointing at, say, the workload-identity token the crawl never saw".
- **Evidence**: `/tmp/aud/t1_nofollow.py` — crawls a fixture mount, swaps the `Projects` directory
  for a symlink to a directory outside the mount, then calls `_read_and_parse` with the `FileRef`
  the crawl produced:

  ```
  crawl saw: Projects/report.txt abs: /tmp/aud/mount/Projects/report.txt
  READ BACK: 'AZURE_CLIENT_SECRET=hunter2  /var/run/secrets/token contents'
  ESCAPED THE MOUNT: True
  ```

  Note also that nothing downstream re-checks: `_file_record` (`sync.py:174`) stores `ref.path`
  verbatim, so the row and the citation both claim `Projects/report.txt`.
- **Fix**: make the containment check part of the same operation as the open. After
  `os.open(..., O_RDONLY | O_NOFOLLOW)`, resolve the descriptor and refuse anything outside the
  mount before reading:

  ```python
  descriptor = os.open(ref.absolute, os.O_RDONLY | os.O_NOFOLLOW)
  actual = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
  if not actual.is_relative_to(mount):        # mount = Path(binding.mount).resolve()
      raise DocumentParseError(f"{ref.path} resolves to {actual}, outside the share mount")
  ```

  `_read_and_parse` must therefore take the mount (it already takes `max_bytes` for the same
  re-check-don't-trust reason). The stronger form — open the mount once and walk components with
  `dir_fd=` plus `O_NOFOLLOW | O_DIRECTORY` — removes the residual `/proc` dependency; the readlink
  check is sufficient here because the descriptor is already open and cannot be re-pointed.

---

## One document off the share can wedge every share's indexing: the embed batch has no size, count or per-item failure bound

- **Severity**: medium
- **Location**: `src/chemclaw/ingest/documents/sync.py:228-258` (`_chunks_for`), called at
  `sync.py:326`; provider at `src/chemclaw/core/embeddings.py:227-237`
  (`_openai_compatible_embeddings`)
- **Trigger**: a single accepted file whose extracted text is large. The only caps in the path are
  per *file* (`max_file_bytes`, default 52,428,800) and per *file count*
  (`document_sync_batch_size`, default 500). Nothing bounds the total extracted text held, the
  number of chunks produced, the size of the single `embed_texts` list, or the memory of the
  returned vectors. `_parse_changed` (`sync.py:199-225`) accumulates every `_Parsed.text` for the
  whole batch first; `_chunks_for` then builds every chunk and calls `embed_texts` **once** with the
  entire list.
- **Consequence**: two distinct failures, both reachable by writing one file to the share.
  (a) Memory: measured 48.6 kB of resident vectors per chunk, so one 50 MB `.txt`/`.csv` is ~2.2 GB
  of embeddings on top of the text and the chunk strings; a full 500-file pass is ~109 GB. The
  worker is OOM-killed with no counter and no report.
  (b) Provider refusal: under `openai_compatible` the whole list goes out as one
  `client.embeddings.create(input=texts)` with no batching, so a real endpoint's per-request input
  limit rejects it. Nothing in `_chunks_for` or `sync_share` catches that, so the activity fails.
  Because `DocumentShareSyncWorkflow` states it "starts from the top of each share rather than from
  a stored cursor" (`durable/document_sync.py:206-210`) and the crawl is deterministic, the same
  batch is rebuilt and fails identically on every retry and every scheduled run — and shares are
  drained sequentially from `state.remaining`, so *all* shares stop indexing.
  This is exactly the wedge `reembed_stale` documents and defends against on the other path
  (`sync.py:371-379`, "One chunk must not starve the whole corpus", with per-chunk retry). The
  crawl path has no equivalent.
- **Evidence**: `/tmp/aud/t4c.py`

  ```
  5.9 MB unique text -> 4369 chunks, 4369 vectors dim 1536
  held: 217 MB   per chunk: 48.6 kB
    one 50 MB file: 43,690 chunks in ONE embed_texts() list = 2.2 GB of vectors
    500 x 5 MB files (one pass): 2,184,500 chunks in ONE embed_texts() list = 108.6 GB of vectors
  ```

  (A first attempt, `/tmp/aud/t4b.py`, measured 0.1 kB/chunk — the repeating filler text collapsed
  to two unique strings in `embed_texts`'s dedup, so the vectors were shared references. The number
  above uses unique text per chunk, which is what a real document produces.)
- **Fix**: bound the batch inside `_chunks_for` rather than relying on the file cap:
  slice `pending` into fixed-size groups (a new `document_embed_batch_size` setting, defaulting to
  something the provider accepts — e.g. 256) and call `embed_texts` per group, appending results;
  and cap chunks per document (`document_max_chunks_per_file`), counting the overflow into a new
  `SyncReport` field so the truncation is visible rather than silent. Wrapping the per-group call in
  the same per-item fallback `_reembed_individually` already implements would additionally stop one
  unembeddable chunk from failing the pass.

---

## A deep directory tree raises `RecursionError`, which the crawl's `except OSError` net does not catch

- **Severity**: medium
- **Location**: `src/chemclaw/ingest/documents/crawl.py:186-216` (`_Walk.descend`, self-recursive at
  line 208) and `crawl.py:270-276` (the `try/except OSError` that is meant to contain a bad root)
- **Trigger**: any subtree nested deeper than the interpreter's recursion limit (1000 by default).
  1200 one-character directory levels is ~2.4 kB of path — well under `PATH_MAX`, and creatable by
  any share member with `mkdir; cd` in a loop. `descend` has no depth cap and recurses once per
  level.
- **Consequence**: `RecursionError` is a `RuntimeError`, not an `OSError`, so it passes straight
  through the handler at `crawl.py:273` that exists to turn an unwalkable root into
  `failed_roots` + "prune nothing". It propagates out of `crawl_share`, out of
  `asyncio.to_thread(...)` in `sync_share` (`sync.py:282`), and fails the
  `sync_document_share` activity. Because the workflow starts each run from the top of the share
  and `descend` descends directories **before** consulting the resume cursor (the
  `if self.after and relative <= self.after` test at `crawl.py:212` is reached only for non-directory
  entries), every retry and every subsequent scheduled run re-enters the same subtree. Indexing for
  that share — and, since `state.remaining` is drained sequentially, for every other share behind it
  — stops permanently. `sync_document_share` carries no bad-data retry policy, so Temporal retries
  the failing activity indefinitely.
- **Evidence**: `/tmp/aud/t2_depth.py`

  ```
  built depth 1200 recursionlimit 1000 NOFILE 20000
  RAISED: RecursionError | is OSError? False | bases: ['RecursionError', 'RuntimeError', 'Exception', 'BaseException']
     msg: maximum recursion depth exceeded while calling a Python object
  ```

  A related, milder case measured in `/tmp/aud/t2b_loop.py`: with `follow_symlinks: true`, a symlink
  pointing at an ancestor **inside** the mount passes `_within_mount` (it does resolve inside the
  mount — containment is checked, cycles are not) and the walk spins until `ELOOP`. That one *is*
  caught, but the pass indexes one file 41 times under 41 synthetic paths and permanently marks the
  root failed, so `prune_share` can never sweep that source again.
- **Fix**: two lines. Give `_Walk` a `depth` and refuse below a configured maximum, recording the
  directory in `result.failed_roots` (which already means "delete nothing this run") rather than
  raising; and widen the guard in `crawl_share` from `except OSError` to
  `except (OSError, RecursionError)` so a walk that blows the stack degrades to a failed root
  instead of a dead activity. Tracking visited `(st_dev, st_ino)` pairs closes the symlink-cycle
  variant at the same time.

---

## A document's own body can forge the citation coordinate, contrary to the comment that says it cannot

- **Severity**: low
- **Location**: `src/chemclaw/ingest/documents/chunk.py:17-26` (`_LABEL`) and `chunk.py:40-56`
  (`_blocks`); rendered into evidence at `retriever.py:227`
- **Trigger**: any indexed document whose extracted text contains a line `[page N]`, `[slide N]` or
  `[sheet X]` at the start of a `\n\n`-delimited block. For `.txt`, `.md`, `.csv`, `.tsv`, `.docx`
  and `.xlsx` this is raw file content — `_parse_text`/`_parse_csv`/`_parse_docx` emit no such
  labels at all. Inside a PDF, one page's *body* can open a block that claims a different page.
- **Consequence**: the chunk is stored with, and cited under, a coordinate the parser never issued.
  `ShareDocumentRetriever._chunks` renders `source=f"{hit.path} [{hit.coordinate}]"`, so a chemist
  is told to verify a claim at `Projects/acme/notes.txt [page 12]` — a plain text file with no
  pages — or at page 7 of a PDF whose page 7 says something else. The forged label line is also
  stripped from the chunk body (`chunk.py:51`), so the marker never reaches the reader; the citation
  simply lies. The comment at `chunk.py:25` states the opposite as a settled property: "A document
  cannot forge a coordinate it was never given." The anchoring to three words fixed accidental
  adoption of `[Figure 2: …]`; it does not stop a document from writing the three real words.
- **Evidence**:

  ```
  content_type: text/plain
  txt  -> coordinate= ''         | content= 'innocuous header'
  txt  -> coordinate= 'page 12'  | content= 'All batches passed release. Signed, QA.'
  pdf  -> coordinate= 'page 1'   | content= 'real page one text'
  pdf  -> coordinate= 'page 7'   | content= 'forged section'
  pdf  -> coordinate= 'page 2'   | content= 'real page two'
  ```
- **Fix**: the coordinate is the parser's knowledge, not the text's, so stop recovering it by
  re-parsing the text. Have `parse_document` return the labelled blocks as structured data
  (`list[tuple[coordinate, body]]`) alongside `text`, and have `chunk_document` consume that instead
  of re-scanning for `_LABEL`. If that is too large a change to make here, the cheap correct
  narrowing is to pass the content type into `chunk_document` and only honour `page`/`slide`/`sheet`
  labels for `application/pdf`, the two presentation/spreadsheet types respectively — and, within a
  PDF, to reject a label whose number is not monotonically increasing.

---

## Exclusion globs are case-sensitive against a case-insensitive file server, so an excluded folder is indexed and cited

- **Severity**: low
- **Location**: `src/chemclaw/ingest/documents/crawl.py:80-101` (`_is_excluded`), consumed at
  `crawl.py:200`
- **Trigger**: the shipped manifest's own pattern `**/Archive/**`
  (`src/chemclaw/ingest/sources/sharedrive/datasource.yaml`) against a folder the file server
  stores as `ARCHIVE` or `archive`. SMB/CIFS treats those as one name; `fnmatch` treats them as
  three strings.
- **Consequence**: the operator's stated intent — keep this subtree out of the corpus — silently
  fails, and the subtree's documents are parsed, embedded and returned as cited evidence. This is
  the same failure mode the function's own docstring records for the earlier `**/` bug ("An operator
  who excluded a folder to keep it out of the corpus got it indexed and cited"), still live for the
  case axis. The docstring acknowledges it; the acknowledgement does not make the corpus smaller.
- **Evidence**:

  ```
  indexed despite the exclusion: ['Projects/ARCHIVE/secret.txt', 'Projects/archive/secret.txt']
    _is_excluded('Projects/Archive/secret.txt') = True
    _is_excluded('Projects/ARCHIVE/secret.txt') = False
    _is_excluded('Projects/archive/secret.txt') = False
  ```
- **Fix**: match case-insensitively — `fnmatch.fnmatch` is already case-insensitive only on
  Windows, so use `fnmatch.fnmatch(relative.lower(), pattern.lower())` for the three forms. The
  stated objection (case-folding "would quietly widen exclusions a deployment already relies on")
  argues for making it explicit rather than for leaving it wrong: add
  `case_sensitive_exclusions: bool = False` to `DocumentShareBinding` so the widening is a value in
  the manifest, and have `make datasource-validate` report which paths a change would newly exclude.

---

## What I checked and found sound

- **The entitlement gate has no alternate path.** `ShareDocumentRetriever._entitled`
  (`retriever.py:116-126`) is the only caller-facing entrance; `grep` across `src/` shows
  `document_chunks`/`document_files` are touched only by this package, `durable/document_sync.py`
  and `cli/sync_share.py` (both operator/background, no query surface). `required_roles` cannot be
  omitted — `DocumentShareBinding._is_coherent` (`binding.py:204-217`) rejects a binding that is
  neither `public: true` nor role-gated, so the "forgot the gate" default really is closed. A gated
  share with no ambient actor returns `[]` rather than falling through.
- **No role widening via the report path.** `ReportRequest.requested_roles` is the one place a
  background run inherits a caller's roles; it is populated from `sorted(get_current_roles())` in
  `agent/durable_tools.py:190`, not from a tool argument, so the model cannot name roles it does not
  hold.
- **No SQL injection.** Every statement in `index.py` and `external_index.py` is a module constant
  with bound parameters, including the tag/date filters and the `unnest(%(docs)s::text[], …)`
  resolve. `core/fulltext.TSQUERY_TERMS` splits Postgres's *own* rendering of
  `websearch_to_tsquery(%(q)s)` — the chemist's query never reaches statement text.
- **No leakage in the surfaced error.** `DocumentIndexError` (`index.py:948-951`) carries a fixed
  string and puts the driver text on `__cause__`, which matters because `api/middleware` relays a
  `SubsystemUnavailableError`'s message verbatim. Log lines in this slice carry paths and counts,
  no content and no credentials.
- **The zip-expansion residual is not exploitable through `zipfile`.** `_refuse_a_bomb`
  (`parse.py:64-92`) reads `file_size` from the central directory and its comment states that a
  crafted archive can understate it. Measured (`/tmp/aud/t3_zip.py`): patching the central
  directory's uncompressed-size field to 100 does make `_refuse_a_bomb` accept a 300 MB payload —
  but `zipfile.ZipExtFile` caps its read at that same declared size, so the read returns 100 bytes
  and then raises `BadZipFile: Bad CRC-32`, which `parse_document`'s boundary net converts to a
  counted `DocumentParseError`. Peak memory: 100 bytes. The residual is real as written and inert
  in practice; the honest-sizes path is genuinely bounded by `document_max_expanded_bytes`.
- **Content is not shared across shares.** `document_chunks` has no `source` column, but both
  searches gate on `_ELIGIBLE` / `_eligible_cuttings`, which require a `document_files` row with
  `f.source = %(src)s` **and** a matching `chunking_key`. A document present only on a gated share
  is unreachable from a public one even when both use the same chunk size.
- **The crawl's own symlink guards hold when nothing races them.** With `follow_symlinks: false` a
  symlink entry is skipped; a root that is a symlink out of the mount is caught by `_within_mount`
  (`crawl.py:262`) before any `scandir`. The gap is the read-time one reported above.
