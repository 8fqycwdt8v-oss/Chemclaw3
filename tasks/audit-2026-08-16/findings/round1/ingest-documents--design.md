# `src/chemclaw/ingest/documents/` — design & simplification

Lens: structure that costs more than it buys. All nine files read in full
(`binding`, `crawl`, `formats`, `parse`, `chunk`, `index`, `external_index`, `sync`, `retriever`),
plus the two consumers outside the slice (`durable/document_sync.py`, `cli/sync_share.py`) needed to
tell dead code from dynamically-reached code. Scripts run under `/tmp/cw/`, output quoted inline.

---

## `ExternalVectorDocumentIndex.prune_stale` is a copy of the base's, and the hook that deletes it already exists

- **Severity**: high
- **Location**: `src/chemclaw/ingest/documents/external_index.py:202` (`ExternalVectorDocumentIndex.prune_stale`)
  vs `src/chemclaw/ingest/documents/index.py:873` (`PostgresDocumentIndex.prune_stale`) and
  `src/chemclaw/ingest/documents/index.py:710` (`PostgresDocumentIndex._forget_vectors`)
- **Trigger**: read the two methods side by side. The subclass re-states the base's transaction
  verbatim — same `DELETE FROM document_files WHERE source = %s AND indexed_at < %s`, same
  `rowcount`, same `DELETE FROM document_chunks c WHERE NOT {CLAIMED_SQL}`, same commit, same return
  — and adds exactly two things: `RETURNING c.doc_id, c.chunking_key, c.ordinal`, and a call to the
  external store's `delete`.
- **Consequence**: the base class already carries the template-method hook for precisely this
  ("Told which chunk rows a re-chunk just superseded, so a subclass can drop their vectors …
  Called after the commit, so a subclass never removes vectors for a transaction that then rolled
  back", `index.py:710`), and `upsert` already uses it in exactly this shape
  (`index.py:814-819`: `RETURNING` → `fetchall` → commit → `await self._forget_vectors(...)`). The
  sweep does not, so the *one* rule the module insists must not be spelled twice — what an orphan
  is, and when its vectors go — is spelled twice anyway, 33 lines apart, and the second copy has no
  test of its own (`prune_stale` appears in the suite only against `PostgresDocumentIndex`,
  `tests/test_document_share.py:1654`; every `prune_share` test runs on `InMemoryDocumentIndex`).
  The comment inside the override even records that these two copies *have* already diverged once
  ("which they briefly did, when this said only `f.doc_id = c.doc_id`").
- **Evidence**: I applied the merge and ran the suite. The change is: base `prune_stale` gains
  `RETURNING c.doc_id, c.chunking_key, c.ordinal` + `fetchall` + `await self._forget_vectors(...)`
  after the commit; `ExternalVectorDocumentIndex.prune_stale` (33 lines) is deleted entirely.

  ```
  $ uv run pytest tests/test_vector_store.py tests/test_document_share.py -q
  97 passed in 3.16s
  $ uv run mypy --strict src/chemclaw/ingest/documents/
  Success: no issues found in 10 source files
  ```

  (Working tree restored afterwards — `git status --porcelain` clean.)
- **Fix**: exactly the patch above. Behaviour-preserving: `PostgresDocumentIndex._forget_vectors` is
  a no-op, so adding the `RETURNING` and the hook call to the base changes nothing for the pgvector
  deployment, and the external deployment inherits byte-identical SQL plus the identical
  `self._store.delete(...)` it wrote by hand.

---

## A bounded crawl pass re-walks everything behind the cursor, so a drain is O(passes × share)

- **Severity**: high
- **Location**: `src/chemclaw/ingest/documents/crawl.py:186` (`_Walk.descend`), specifically the
  directory branch at `:207-210` and the cursor test at `:211-213`
- **Trigger**: any drain that takes more than one pass — i.e. every first crawl of the TB share this
  module was written for. `descend` unconditionally recurses into every directory and only *then*
  drops individual files with `if self.after and relative <= self.after: continue`. There is no test
  that lets it skip a subtree it has already consumed, even though the walk is a total order and the
  cursor is a position in it.
- **Consequence**: the module docstring's cost model ("A crawl of 500k files that reads nothing is a
  `scandir` pass measured in minutes") is stated for *one* pass and paid on *every* pass. With
  `document_sync_batch_size` candidates per activity, a K-pass drain does K full walks: K × every
  `scandir`, every `sorted()`, every `PurePosixPath(...).relative_to(...)`, every `is_symlink()` /
  `is_dir()`, every `fnmatch` against every exclusion pattern. On a mounted CIFS volume those are
  network round trips, which is the whole reason the design avoids reading bytes. A 500k-candidate
  share at 1000/pass is 500 full walks of the share to index it once.
- **Evidence**: 200 dirs × 100 candidates = 20,000 files on tmpfs (`/tmp/cw/drain.py`):

  ```
  single pass, 20000 files: 0.283s
  drain in 20 passes, 20000 files: 1.671s
  final pass (returns 100 files): 0.152s
  ```

  The final pass returns 100 of 20,000 files and still costs 54% of a complete walk. Separately
  (`/tmp/cw/bound.py`), `limit` bounds *accepted candidates*, not work: `crawl_share(limit=1)` over a
  directory of 20,000 unreadable `.doc` files returned 1 file having examined all 20,000
  (`unsupported .doc counted: 20000`).
- **Fix**: prune the subtree in the directory branch. Every path a directory yields begins with
  `relative + "/"`, so when the cursor is past that prefix and not inside it, the subtree is entirely
  consumed:

  ```python
  if entry.is_dir(follow_symlinks=self.binding.follow_symlinks):
      prefix = relative + "/"
      if self.after and self.after > prefix and not self.after.startswith(prefix):
          continue                      # wholly behind the cursor
      if not self.descend(Path(entry.path), root):
          return False
      continue
  ```

  Behaviour-preserving: sound because `_order` already keys directories as `name + "/"` so sibling
  order agrees with joined-path order (the module argues this at `crawl.py:169-179`), which is
  exactly the invariant the skip relies on. Verified by running the same drain with the patch
  monkeypatched in (`/tmp/cw/skipfix.py`) and asserting the emitted path list is identical:

  ```
  baseline  20 passes, 20000 files, 1.759s
  with-skip 20 passes, 20000 files, 0.343s
  identical output: True
  ```

  5.1× on tmpfs; the gap widens with directory-listing latency.

---

## An exclusion pattern cannot prune a directory, so an excluded archive is fully enumerated on every pass

- **Severity**: medium
- **Location**: `src/chemclaw/ingest/documents/crawl.py:80` (`_is_excluded`), used at
  `crawl.py:200` before the directory branch
- **Trigger**: the exclusion pattern the shipped manifest carries —
  `src/chemclaw/ingest/sources/sharedrive/datasource.yaml`, `exclude: ["**/Archive/**"]`.
- **Consequence**: `_is_excluded` matches the path, the path with a leading `/`, and the basename.
  None of those matches the *directory* `Projects/Archive`, only the files under it. So the walk
  descends the entire archive tree, lists it, sorts it and fnmatch-tests every entry three times per
  pattern, on every pass, to arrive at the operator's already-stated conclusion that none of it is
  wanted. The docstring's claim that "excluding them is cheaper than parsing them" is true and hides
  that they are still fully *enumerated* — and combined with the finding above, enumerated K times.
  The sharp version: a bare-name pattern would have pruned it, and the pattern the manifest ships
  (and that the docstring at `crawl.py:85-88` explicitly recommends) is the one that does not.
- **Evidence** (`/tmp/cw/excl.py`, and a direct check):

  ```
  'Archive'                        excluded=False
  'Projects/Archive'               excluded=False
  'Archive/old.pdf'                excluded=True
  'Projects/Archive/old.pdf'       excluded=True

  dir 'Projects/Archive' vs '**/Archive/**': False
  dir 'Projects/Archive' vs 'Archive'      : True
  ```

  Output is correct (only `Projects/live.txt` is indexed) — this is cost, not wrongness.
- **Fix**: after resolving `is_dir`, test a sentinel child before descending:

  ```python
  if entry.is_dir(...) and _is_excluded(f"{relative}/￿", self.binding.exclude):
      continue
  ```

  Behaviour-preserving for the "everything under here" patterns this is aimed at, and deliberately
  inert for narrower ones — measured: `**/Archive/**` → sentinel True (and every real child is
  excluded today, so nothing new is dropped); `**/Archive/*.tmp` → sentinel False (still descends,
  correctly); `Projects/Archive/` → sentinel False, so the one pattern whose directory-form matches
  while its children do not is *not* pruned. Residual, stated: a pattern that discriminates on child
  names in a way the sentinel happens to match would newly prune. None of the shipped patterns do.

---

## The share's file-row filter and its bound parameters are spelled three times, in the module that argues against that

- **Severity**: medium
- **Location**: `src/chemclaw/ingest/documents/index.py:580` (`_FILE_MATCH`) and `index.py:889`
  (`_params`) vs `src/chemclaw/ingest/documents/external_index.py:303-317` (`_eligible_cuttings`)
  and `external_index.py:358-366` (`_resolve`)
- **Trigger**: change a filter dimension — add `author`, or make `tag` a list. Four sites must move
  together and nothing links them.
- **Consequence**: `index.py:575-579` states the rule in its own words — "Written once and shared by
  both, rather than spelled twice: eligibility and citation must select over the *same* file rows …
  Two copies of a five-clause predicate is a divergence waiting for whichever of them gets a sixth
  clause first." `_eligible_cuttings` is that sixth-clause risk realised as a third copy: it
  re-types the same `source` / `tag = ANY(tags)` / `modified_at >= since` / `modified_at <= until`
  filter by hand against `document_files`. And the parameter dict `{"src", "tag", "since", "until"}`
  that `_params` exists to produce is re-typed by hand twice more, in `_eligible_cuttings` and in
  `_resolve` (the latter to feed `CITATION_SQL`, which it *imports* rather than re-types — so the
  statement is shared and its bindings are not). The concrete class of bug this invites is the one
  the file already records twice for this exact pair of methods: a scope computed on a different
  predicate from the one that decides eligibility returns hits the citation then drops, silently and
  with nothing raised.
- **Evidence**: `_FILE_MATCH` (`index.py:580-586`) and the `WHERE` of `_eligible_cuttings`
  (`external_index.py:306-310`) carry the same three parameterised clauses in the same order against
  the same table, differing only in the two join clauses `_eligible_cuttings` does not need.
  `_params` (`index.py:891-897`) and the dicts at `external_index.py:311-316` and `:358-366` are the
  same four keys.
- **Fix**: extract the filter clauses once —
  `_FILE_FILTERS = "(%(tag)s::text IS NULL OR %(tag)s = ANY(f.tags)) AND (…since…) AND (…until…)"`
  — build `_FILE_MATCH` from it, and have `_eligible_cuttings` select
  `FROM document_files f WHERE f.source = %(src)s AND {_FILE_FILTERS}`. Have both external methods
  bind `self._params(source, top_k, filters)` (psycopg ignores the unused `k` key) instead of
  rebuilding the dict. Behaviour-preserving — same clauses, same parameter names, same values.

---

## Selecting the share-carrying sources is cloned between the two consumers of `DocumentShareSource`

- **Severity**: medium
- **Location**: `src/chemclaw/ingest/documents/sync.py:68` (`DocumentShareSource`, the protocol whose
  home this is) vs `src/chemclaw/durable/document_sync.py:60` (`share_sources`) and
  `src/chemclaw/cli/sync_share.py:50` (`_resolve`)
- **Trigger**: both consumers need "the enabled sources that carry a share, by name". Neither can
  get it from the slice, so both write it.
- **Consequence**: character-identical comprehensions in two files:
  `{source.name: source for source in active_retrieve_sources() if isinstance(source, DocumentShareSource)}`.
  The protocol's own docstring makes selection a load-bearing rule ("enabling a share stays exactly
  one thing: `CHEMCLAW_DATA_SOURCES`"), and that rule now lives in two places, in two layers, with
  no test tying them. The CLI additionally imports `DocumentShareSource` from `sync.py` *only* to
  re-run this selection — it already imports five other names from that module, so there is no
  layering obstacle to the helper living there.
- **Evidence**: `durable/document_sync.py:66-70` and `cli/sync_share.py:52-56` differ only in the
  variable they assign to.
- **Fix**: move `share_sources()` next to the protocol in `ingest/documents/sync.py` (with the
  `active_retrieve_sources` import at function scope if a module-scope
  `ingest.documents → ingest.sources.registry` edge is unwanted), and have both consumers import it.
  `cli/sync_share.py:_resolve` keeps only its error message. Behaviour-preserving.

---

## `ExternalVectorDocumentIndex.store_embeddings` re-opens the base's transaction instead of using a hook

- **Severity**: low
- **Location**: `src/chemclaw/ingest/documents/external_index.py:178` vs
  `src/chemclaw/ingest/documents/index.py:833` (`PostgresDocumentIndex.store_embeddings`)
- **Trigger**: same shape as the `prune_stale` clone above, with the hook missing rather than unused.
- **Consequence**: the subclass repeats the base's `if not chunks: return`, its `self._connection()`
  block, its per-chunk loop and its commit, to run a statement that differs from
  `self._store_embedding` only in dropping the `embedding = …::vector(N)` assignment — the very
  assignment `_chunk_vector` (`index.py:720`) exists to make optional. Two transaction bodies to keep
  in step for one differing SET clause.
- **Evidence**: `external_index.py:186-200` against `index.py:837-849`; the only structural
  difference is the extra `await self._store.upsert(...)` at `external_index.py:187`.
- **Fix**: add one hook — `async def _publish_vectors(self, chunks)` (no-op on the base, store upsert
  on the subclass) — called at the top of the base's `store_embeddings`, and build
  `self._store_embedding` from `_chunk_vector`'s answer the way `_upsert_chunk` already does. The
  subclass then declares two three-line hooks and no transaction. Behaviour-preserving, with one
  detail to confirm on a live database: the external variant would then write
  `embedding = NULL::vector(N)` where today it leaves the column untouched — it is already NULL
  there (`_chunk_vector` returns `None` on every write), but this is the one line of the refactor
  that wants a run against Postgres rather than an argument.

---

## `@runtime_checkable` on `DocumentIndex` has no `isinstance` user

- **Severity**: low
- **Location**: `src/chemclaw/ingest/documents/index.py:137-138`
- **Trigger**: none — that is the finding.
- **Consequence**: `runtime_checkable` exists to permit `isinstance`. Nothing in `src/` or `tests/`
  ever asks `isinstance(x, DocumentIndex)`; the protocol is used purely as a static type. The
  decorator reads as a signal that some caller dispatches on it (the sibling protocol
  `DocumentShareSource`, `sync.py:68`, genuinely does — two call sites), so a maintainer must check
  before touching it. It also silently weakens nothing but costs a lookup every time someone asks
  "who dispatches on this?".
- **Evidence**:

  ```
  $ grep -rn "isinstance" --include=*.py src/ tests/ | grep -i "documentindex\|DocumentShareSource"
  src/chemclaw/durable/document_sync.py:69:        if isinstance(source, DocumentShareSource)
  src/chemclaw/cli/sync_share.py:55:        if isinstance(source, DocumentShareSource)
  ```

  Two hits, both the *other* protocol; zero for `DocumentIndex`.
- **Fix**: drop the decorator (and its `runtime_checkable` import stays for `DocumentShareSource`).
  Behaviour-preserving — no runtime check exists to break.

---

## A comment in the retriever asserts an exception-ordering constraint that does not exist

- **Severity**: low
- **Location**: `src/chemclaw/ingest/documents/retriever.py:153-160`
- **Trigger**: read the comment, then check the class hierarchy.
- **Consequence**: the comment says "Ordered: `DocumentIndexError` is the narrower type and must be
  tested first." `DocumentIndexError` derives from `SubsystemUnavailableError`, which
  `core/errors.py` documents as deliberately outside the `ChemclawError` tree and which derives
  straight from `Exception` — it is not a subclass of `ConnectionError`, `OSError` or `RuntimeError`
  (its own tuple-mates), and `DocumentShareError` (a `ChemclawError`/`ValueError`) is neither its
  ancestor nor its descendant. Swapping the two `except` clauses changes nothing. The comment
  therefore describes an invariant a maintainer will preserve for no reason, and — Rule 2 — it is a
  claim about the code that the code does not support.
- **Evidence**: `index.py:46` (`class DocumentIndexError(SubsystemUnavailableError)`),
  `binding.py:28` (`class DocumentShareError(ChemclawError)`), `core/errors.py:27,37`
  (`ChemclawError(ValueError)`, `SubsystemUnavailableError(Exception)`).
- **Fix**: replace the sentence with what the split is actually for — the two branches differ only in
  log level and message (`warning` + `debug` for a transient backend, `exception` for a permanent
  misconfiguration). One line, no code change.

---

## `_ZIP_CONTAINERS` re-spells three MIME literals already in the format table

- **Severity**: low
- **Location**: `src/chemclaw/ingest/documents/parse.py:234-240`
- **Trigger**: adding or renaming an OOXML format.
- **Consequence**: the three 80-character OOXML content types now appear in `formats.EXTENSIONS`
  (`formats.py:25-27`), in `_PARSERS` (`parse.py:227-229`) and again in `_ZIP_CONTAINERS`. The
  import-time consistency guard at `parse.py:246-253` checks the first two against each other and
  says so at length ("A format in one and not the other is … both invisible at runtime, both caught
  here at import") — but it does not check the third. A new zip-container format added to the first
  two and forgotten here silently skips the zip-bomb ceiling, which is exactly the invisible-at-
  runtime failure the guard was written for.
- **Evidence**: `_UNPARSEABLE`/`_UNREACHABLE` at `parse.py:246-247` compute over
  `EXTENSIONS.values()` and `_PARSERS` only; `_ZIP_CONTAINERS` is a hand-written frozenset.
- **Fix**: `_ZIP_CONTAINERS = frozenset(EXTENSIONS[e] for e in (".docx", ".xlsx", ".pptx"))` — one
  spelling of the three types, and a `KeyError` at import if one is renamed. Behaviour-preserving.

---

## Checked and found sound (not findings)

Recorded so the absence is evidence rather than silence:

- **`InMemoryDocumentIndex` (`index.py:319-542`, ~225 lines with only test callers).** This looks
  like the classic "second implementation in `src/`" finding and I do not think it is one here. The
  semantics it duplicates are pinned against the real backend by
  `tests/test_document_lexical_rule.py::test_the_two_document_backends_state_the_same_boolean_rule`,
  which loads the same corpus into both and asserts the same hit set and the same top rank against a
  migrated Postgres. The parity test covers the lexical rule only, so the write-path semantics
  (`upsert`'s unclaimed-cutting drop, `prune_stale`'s orphan rule, `known_documents`) are still two
  independent statements with no parity test — worth one, but that is a test-coverage finding rather
  than a design one.
- **`DocumentShareBinding.public` (`binding.py:132`) has no reader outside its own validator.** It is
  not dead: it makes "ungated" something the manifest *says* rather than omits, and
  `retriever._entitled` reading `required_role_set` alone is equivalent because
  `_is_coherent` (`binding.py:204-217`) refuses both-set and both-unset. Intentional and correct.
- **`formats.py`'s zero-import split**, `parse.py`'s single `except Exception` at the parse boundary,
  the `_forget_vectors`/`_chunk_vector`/`_require_vector_column` hook set, and the
  `chunk._LABEL` anchoring to the three words the parsers emit — all checked, all earn their
  structure.
- **Dynamic registration**: `sync_document_share` / `reembed_stale_documents` /
  `prune_document_share` / `plan_document_sync` are reached through Temporal's
  `@durable_activity`/`@activity.defn` registry, and `ShareDocumentRetriever` is reached by dotted
  string from `sharedrive/datasource.yaml` (`retrieve: chemclaw.ingest.documents.retriever:ShareDocumentRetriever`).
  Nothing in this slice is dead by that test.
