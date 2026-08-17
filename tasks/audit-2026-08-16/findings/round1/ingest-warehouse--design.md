# Round 1 — `ingest/eln/**` + `ingest/sources/**` — design & simplification

Slice read in full: `eln/adapter.py`, `eln/json_adapter.py`, `eln/ord_adapter.py`, `eln/ord.py`,
`eln/warehouse/{__init__,binding,expr,sql,adapter,retriever,connect,driver,snowflake}.py`,
`sources/{__init__,base,manifest,registry,vendored_dataset}.py` and all seven `datasource.yaml`
manifests. `eln/sync.py` and `durable/eln_sync.py` were read as context for what the adapters'
outputs are consumed by, not audited.

Three of the six findings are the same shape: **two implementations of one rule, where the rule was
extracted into a shared helper but the *use* of it was not**, and the copies have since drifted apart
in ways that lose data silently. That is the through-line worth carrying out of this slice.

---

## The two file-drop adapters' `fetch_new_entries` are a 30-line clone, and it has already diverged

- **Severity**: high
- **Location**: `src/chemclaw/ingest/eln/json_adapter.py:116-158` (`JsonExportAdapter.fetch_new_entries`)
  vs `src/chemclaw/ingest/eln/ord_adapter.py:84-131` (`OrdJsonAdapter.fetch_new_entries`); the divergent
  line is `json_adapter.py:142`.
- **Trigger**: any `*.json` file in `settings.eln_export_dir` that is not valid UTF-8 — an export
  written by a tool emitting latin-1/cp1252, which is exactly the case `ord_adapter.py:110-115`
  documents as having happened once already.
- **Consequence**: `Path.read_text(encoding="utf-8")` raises `UnicodeDecodeError`, which derives from
  `ValueError` — not from `OSError`, and `json.JSONDecodeError` is a *sibling* subclass, not a parent.
  `json_adapter` catches `(OSError, json.JSONDecodeError, ElnFormatError)`, so the exception escapes
  the loop, escapes `fetch_new_entries`, and aborts the whole batch: every good export file sorting
  after the bad one is never read, on this run and on every run after it. The adapter's own docstring
  ("one broken export file must not abort the whole fetch") is false. `ord_adapter` catches
  `UnicodeDecodeError` explicitly and carries a six-line comment explaining why — the fix was made in
  one copy of the clone and not the other, and `tests/test_eln.py:1698` is the regression test for the
  ORD copy with no counterpart for the JSON one.
- **Evidence**: the two bodies are structurally identical — same `for path in sorted(self._dir.glob("*.json"))`,
  same `isinstance(payload, dict)` guard, same `entry_window(...) >= since` / `elif is_late_arrival(...)`
  split, same `warn_late_arrivals(...)` + `entries.sort(key=lambda e: e.created_at)` tail. They differ
  in four expressions: the source label, the two timestamp readers, the `entry_id` expression, and the
  caught exception tuple. Four helpers (`parse_iso_utc`, `entry_window`, `is_late_arrival`,
  `warn_late_arrivals`) were already extracted into `eln/adapter.py` — the leaves were factored out and
  the trunk was left duplicated.

  `/tmp/wh/repro1.py` — a directory holding one latin-1 file (`a_bad.json`) and one perfectly good file
  after it (`b_good.json`), given to each adapter:

  ```
  json_adapter  -> RAISED UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe9 in position 58: invalid continuation byte
                 the good file after it was never read
  skipping unreadable ORD export a_bad.json: 'utf-8' codec can't decode byte 0xe9 in position 29: invalid continuation byte
  ord_adapter   -> OK, entries: ['good']
  ```

- **Fix**: add one scanner to `eln/adapter.py` beside the four helpers it already owns, and make both
  adapters call it:

  ```python
  def scan_json_exports(
      directory: Path,
      since: datetime,
      *,
      source: str,
      logger: Logger,
      format_error: type[ElnMappingError],
      read_stamps: Callable[[dict[str, Any], Path], tuple[datetime, datetime | None]],
      read_entry_id: Callable[[dict[str, Any], Path], str],
  ) -> list[RawEntry]: ...
  ```

  with the single `except (OSError, UnicodeDecodeError, json.JSONDecodeError, format_error)`. Each
  adapter's `fetch_new_entries` becomes six lines. Behaviour-preserving for `ord_adapter`; for
  `json_adapter` it changes behaviour exactly where the behaviour is wrong today — a non-UTF-8 file
  becomes a skip plus a WARNING instead of an aborted batch. It also removes the class of defect
  rather than one instance of it: the next `except` clause fixed is fixed for both sources.

  *Related, same file:* the package README and `warehouse/binding.py:4` both open with "`json_adapter`
  knows a payload has `reaction_smiles`". It does not — `json_adapter` reads `reactants`, `products`,
  `procedure`, `operator`; the string `reaction_smiles` appears nowhere in it. The argument the
  sentence supports is sound, the example in it is invented.

---

## `warehouse/adapter._optional_timestamp` silently drops an unparseable `modified_at`, wedging the sync cursor forever

- **Severity**: high
- **Location**: `src/chemclaw/ingest/eln/warehouse/adapter.py:386-398` (`_optional_timestamp`), reached
  from `_raw_entry` at `adapter.py:161-163`. Its same-named twin is
  `src/chemclaw/ingest/eln/json_adapter.py:372-379`.
- **Trigger**: a warehouse whose declared `modified_at` column arrives at Python as a string the ISO
  reader rejects. Measured spellings that fail: Snowflake's `TIMESTAMP_TZ` string form
  `'2026-08-01 12:00:00.000 -0700'` (space before the offset), and any site VARCHAR audit column
  (`'01/08/2026 12:00'`). Both are ordinary, and neither is a corrupt row.
- **Consequence**: a permanent, completely silent sync wedge. `_optional_timestamp` swallows the
  `ValueError` and returns `None`, so `RawEntry.modified_at` is `None` and
  `sync_entries` advances its cursor to `entry_window(created_at, None) == created_at`. But the *fetch*
  filters on `COALESCE(MODIFIED_AT, CREATED_AT)` inside the warehouse, where the same value parses
  fine — so the row is returned again on the next run, and the next, forever. The batch is never
  truncated by the workflow's reckoning either, so `durable/eln_sync.py`'s wedge guard is never
  reached; the log reads `ingested=N rejected=0` on every run. This is precisely the failure
  `sync.py:141-149` describes and believes it has closed.
- **Evidence**: the two `_optional_timestamp` functions carry the same name, take the same argument
  and implement opposite policies. `json_adapter`'s is explicit about which one is correct:

  > "A *present but unparseable* value is bad data and is raised, because silently treating it as
  > absent would reinstate the exact silence this field exists to break."

  The warehouse copy does the thing that docstring forbids:

  ```python
  try:
      return parse_iso_utc(text)
  except (ValueError, TypeError):
      return None
  ```

  `/tmp/wh/repro2b.py` — one row, `CREATED_AT` ISO, `MODIFIED_AT` in Snowflake's `TIMESTAMP_TZ` string
  form, against a fake warehouse that applies `WHERE/ORDER BY/LIMIT` the way the warehouse itself
  would (it parses its own stamps); the loop advances the cursor exactly as `sync_entries` does:

  ```
  run 1: since=2020-01-01T00:00:00 fetched=['R-1'] created_at=2026-01-01T00:00:00 modified_at=None
  run 2: since=2026-01-01T00:00:00 fetched=['R-1'] created_at=2026-01-01T00:00:00 modified_at=None
  run 3: since=2026-01-01T00:00:00 fetched=['R-1'] created_at=2026-01-01T00:00:00 modified_at=None
  run 4: since=2026-01-01T00:00:00 fetched=['R-1'] created_at=2026-01-01T00:00:00 modified_at=None

  statement: SELECT * FROM V_RXN WHERE COALESCE(MODIFIED_AT, CREATED_AT) >= ? ORDER BY COALESCE(MODIFIED_AT, CREATED_AT) ASC LIMIT ?
  the row's real amendment: 2026-08-01T12:00:00-07:00 -- never reaches RawEntry
  ```

  With `fetch_limit` rows in that state the page never empties and no later reaction is ever ingested.
- **Fix**: there should be one reader of an optional source timestamp, not three
  (`json_adapter._optional_timestamp`, `warehouse/adapter._optional_timestamp`, `expr._iso_datetime` —
  all three do `parse_iso_utc`, all three differ on what a bad value means). Put it in `eln/adapter.py`
  next to `parse_iso_utc`:

  ```python
  def read_optional_stamp(value: Any, *, what: str, error: type[ElnMappingError]) -> datetime | None:
      """None when absent; raises when present and unreadable — silence must not be forgeable."""
  ```

  and have `warehouse/adapter._raw_entry` use it. Not behaviour-preserving, deliberately: it converts a
  silent permanent wedge into a per-row `ElnMappingError`, which `sync_entries`' reject-and-continue arm
  already reports by id and reason. `expr._iso_datetime` keeps its `TransformError` wrapper but should
  call the same reader, so the three cannot drift again.

---

## `sql.watermark_expression` implements `COALESCE` while `adapter.entry_window` — the definition it cites — implements `max`

- **Severity**: medium
- **Location**: `src/chemclaw/ingest/eln/warehouse/sql.py:45-55` (`watermark_expression`) vs
  `src/chemclaw/ingest/eln/adapter.py:82-94` (`entry_window`).
- **Trigger**: any row where `modified_at < created_at`. The realistic case is not clock skew: it is a
  site whose amendment column has *day* granularity (a `DATE`, or a nightly ETL stamp) while the
  creation column is a `TIMESTAMP`. Every row created after midnight on the day it was last touched
  has `modified < created`.
- **Consequence**: the row is excluded from the fetch entirely. `COALESCE(modified, created)` takes the
  *smaller* value, so a reaction created after the stored cursor is not returned; the cursor then moves
  past it on the next real row and it is never ingested. `entry_window`'s `max` can only ever widen the
  window, never narrow it — the two file adapters would have kept this row. The ordering half is worse
  in the same way: `ORDER BY COALESCE(...) ASC LIMIT n` sorts such a row into a page that has already
  been drained, so under paging it is skipped without any granularity mismatch at all.
- **Evidence**: `watermark_expression`'s docstring names `entry_window` as the rule it honours
  ("`chemclaw.ingest.eln.adapter.entry_window` says so, and both file-drop adapters honour it"), while
  `entry_window`'s docstring names `COALESCE` as the alternative it deliberately rejected ("`max` rather
  than 'modified if present, else created'"). The SQL implements the rejected alternative.

  `/tmp/wh/repro3.py`:

  ```
  SQL watermark : COALESCE(LAST_MODIFIED_TS, CREATED_TS)
  SQL statement : SELECT * FROM V_RXN WHERE COALESCE(LAST_MODIFIED_TS, CREATED_TS) >= ? ORDER BY COALESCE(LAST_MODIFIED_TS, CREATED_TS) ASC LIMIT ?

  created=14:00 modified=00:00 cursor=12:00
    entry_window (max, the rule the docstrings cite) = 14:00  -> fetched? True
    COALESCE     (what watermark_expression emits)   = 00:00  -> fetched? False
  ```

- **Fix**: emit the SQL spelling of `max`, guarding the NULL case (`GREATEST` returns NULL if any
  argument is NULL in Snowflake):

  ```python
  return f"GREATEST({entry.created_at}, COALESCE({entry.modified_at}, {entry.created_at}))"
  ```

  Behaviour-preserving for every row where `modified >= created` or `modified IS NULL`, i.e. every row
  the two expressions agree on today; it changes only the rows they disagree on, and there the new
  answer is the one `entry_window` documents. Add the `modified < created` row to
  `tests/test_warehouse_adapter.py`'s watermark case — the current fixture only exercises agreement.

---

## The closed transform vocabulary is a table plus three hard-coded `if name == ...` special cases

- **Severity**: low
- **Location**: `src/chemclaw/ingest/eln/warehouse/expr.py:253-309` (`_Transform`, `TRANSFORMS`,
  `validate_transform`).
- **Trigger**: adding a twelfth transform that needs any load-time check of its options.
- **Consequence**: `_Transform` was built to carry everything a transform declares — `apply`,
  `required`, `optional` — and then three transforms turned out to need one thing more, so
  `validate_transform` ends with a tail of `if name == "clamp"`, `if name == "value_map"`,
  `if name == "regex"` that the table does not know about. The table is no longer the vocabulary; the
  table plus the tail is. A new transform's author reads `TRANSFORMS`, sees a complete-looking
  declaration, and does not learn that per-transform validation lives 40 lines below in a chain of name
  comparisons. This is small today and grows linearly with the vocabulary, which is the whole reason the
  vocabulary is a table.
- **Evidence**: `expr.py:304-309`

  ```python
  if name == "clamp" and not set(options):
      raise PathSyntaxError("transform 'clamp' needs at least one of 'min' or 'max'")
  if name == "value_map":
      _check_map_keys(options["map"])
  if name == "regex":
      _check_pattern(options)
  ```

  The two helpers `_check_map_keys` and `_check_pattern` are already single-caller functions; only the
  dispatch is missing. (Minor, same dataclass: `required: frozenset[str] = frozenset()` and
  `optional: frozenset[str] = field(default_factory=frozenset)` are two spellings of a default for one
  immutable type — `frozenset()` is fine for both.)
- **Fix**: one more field on `_Transform`, and the tail becomes a call.

  ```python
  @dataclass(frozen=True)
  class _Transform:
      apply: Callable[[Any, Mapping[str, Any]], Any]
      required: frozenset[str] = frozenset()
      optional: frozenset[str] = frozenset()
      check: Callable[[Mapping[str, Any]], None] | None = None
  ```

  `"clamp": _Transform(_clamp, optional=..., check=_check_clamp)` etc.; `validate_transform` ends with
  `if transform.check: transform.check(options)`. Behaviour-preserving.

---

## `sources/base.py`: a protocol with one implementation and no runtime caller, and a re-export convention nothing follows

- **Severity**: low
- **Location**: `src/chemclaw/ingest/sources/base.py:24-48` (`__all__`, `DataSource`) and
  `base.py:63-66` (`SourceSpec.__post_init__`).
- **Trigger**: reading the module to learn the seam's contract.
- **Consequence**: three of the four things this 66-line module declares are inert.
  1. `DataSource` (the `Protocol`) has exactly one implementation, `SourceSpec`, in the same file. Its
     only non-annotation use anywhere in the tree is `assert isinstance(source, DataSource)` in
     `tests/test_datasource_seam.py:94` — a test that asserts a protocol against the one class written
     to satisfy it. `make_data_source` is annotated `-> DataSource` and returns a `SourceSpec`.
  2. `RawEntry` and `EvidenceChunk` are re-exported with the stated purpose "so a source module imports
     them from the seam, not from two subsystems". No production module does. `warehouse/adapter.py:22`
     imports `RawEntry` from `eln.adapter`; `warehouse/retriever.py:43` and
     `sources/vendored_dataset.py:35` import `EvidenceChunk` from `retrieval.evidence`;
     `durable/eln_sync.py:31` imports `RawEntry` from `eln.adapter`. The convention has zero adherents,
     including inside its own package, and one importer total — a test.
  3. `SourceSpec.__post_init__`'s "neither half" guard is unreachable in production: the only
     construction site is `registry.make_data_source`, from a manifest that
     `DataSourceManifest._must_provide_a_half` has already rejected for the same reason. The module
     docstring at `manifest.py:89-91` acknowledges this ("`SourceSpec` is also constructed directly in
     tests") — which is the definition of a guard kept alive by a test that calls it directly.
- **Evidence**: `rg "from chemclaw.ingest.sources.base import"` returns four hits: `registry.py`,
  `durable/eln_sync.py` (`IngestHalf` only), and two tests. `rg "import.*RawEntry"` outside tests
  returns three hits, all pointing at `eln.adapter`, one of them being `base.py` itself.
- **Fix**: delete `DataSource` and the `RawEntry`/`EvidenceChunk` entries from `__all__`, annotate
  `make_data_source(name) -> SourceSpec`, and drop `__all__` to the three names that are actually the
  seam (`SourceSpec`, `IngestHalf`, `RetrieveHalf`). Behaviour-preserving apart from deleting the one
  `isinstance` assertion in `test_datasource_seam.py`, which asserts nothing about production
  behaviour. Keep `SourceSpec.__post_init__` or delete it, but do not keep both it and the manifest
  validator while calling them "twins" — one of them has a caller.

---

## The two warehouse halves each carry their own lazy-connect, and the adapter's is `async` with nothing to await

- **Severity**: low
- **Location**: `src/chemclaw/ingest/eln/warehouse/adapter.py:79-83` (`WarehouseElnAdapter._connection`)
  and `src/chemclaw/ingest/eln/warehouse/retriever.py:75-79` (`WarehouseVectorRetriever._connection`).
- **Trigger**: reading either half, or adding a third one.
- **Consequence**: two identical four-line bodies, and they disagree about whether connecting is
  asynchronous. `open_warehouse` is a plain synchronous function (`connect.py:87`) — it resolves an
  import and reads environment variables; the *vendor* connection is opened lazily inside
  `SnowflakeWarehouse._connect`, not here. So the adapter's `async def` awaits nothing, forces
  `await self._connection()` at its one call site, and tells a reader that opening a warehouse is I/O
  at this layer when it is not. `connect.py`'s own docstring says it exists "shared by both halves
  because both connect the same way and neither should own the other's copy of it" — and then each half
  owns its own copy of the memoisation around it.
- **Evidence**:

  ```python
  # adapter.py
  async def _connection(self) -> Warehouse:
      if self._warehouse is None:
          self._warehouse = open_warehouse(self._binding.connection)
      return self._warehouse

  # retriever.py
  def _connection(self) -> Warehouse:
      if self._warehouse is None:
          self._warehouse = open_warehouse(self._binding.connection)
      return self._warehouse
  ```

  Byte-identical apart from `async`. Neither body contains an `await`.
- **Fix**: drop `async` from the adapter's (`warehouse = self._connection()` at `adapter.py:93`), which
  makes the two literally the same function, and move it into `connect.py` as a tiny holder if a third
  half ever appears — not before (Rule of Three). Behaviour-preserving: no caller of either method
  depends on it yielding to the loop.

---

## Minor — three one-line smells, listed rather than sectioned

- `ord_adapter.py:319-323`: `for wanted in ("SMILES",): for kind, value in identifiers: if kind == wanted`
  is a loop over a one-element tuple, left behind when the multi-identifier resolution below it was
  added. It is four lines that mean `next((v for k, v in identifiers if k == "SMILES"), None)`.
- `ord_adapter.py:497-509`: `_optional_list` (raises on a non-list) and `_as_list` (silently yields
  nothing) are two policies for one question, applied inconsistently — `outcomes`/`workups` raise,
  `products`/`components`/`identifiers`/`measurements` do not. Consequence is a misdiagnosing message:
  an ORD file with `"products": {...}` (an object where the list belongs) reaches
  `raise OrdFormatError("ORD reaction has no products")`, which is not what is wrong with it.
- `sources/vendored_dataset.py:139`: `name: str = "vendored"` still carries the literal default that
  `registry._build_retrieve_half`'s docstring documents as the defect it removed ("Nothing used to
  supply it, so the three *parameterised* halves each answered with a literal default"). The warehouse
  retriever was fixed (`retriever.py:60-65` makes `name` required and explains why); this one was not.
  Dead today because `_build_retrieve_half` always passes it — but it is exactly the parameter the
  fix's own rationale says must not have a default.

---

## Checked and clean, through this lens

- **Dead code**: every public symbol in the slice has a live non-test caller, checked including the
  dynamic paths — `datasource.yaml`'s `ingest:`/`retrieve:` and `connection.driver` are
  `module:callable` strings resolved by `registry.resolve_half` / `connect._resolve_driver`, so
  `JsonExportAdapter`, `OrdJsonAdapter`, `WarehouseElnAdapter`, `WarehouseVectorRetriever`,
  `VendoredDatasetRetriever` and `SnowflakeWarehouse` are all reachable only through YAML and are not
  dead. `active_ingest_sources` (durable/memory_jobs), `active_ingest_source_names`,
  `active_retrieve_sources`, `make_data_source`, `resolve_half`, `normalise_score`, `SCORE_COLUMN`,
  `template_paths`, `render_template`, `as_text` all have production callers.
- **Layering**: no cycle. `ingest` imports `retrieval.evidence` and `kg.note`; `retrieval` imports
  nothing from `ingest` (`rg "from chemclaw.ingest" src/chemclaw/retrieval/` is empty). The one
  higher-layer reach, `warehouse/retriever.py` → `kg.note.note_relative_path`, is a deliberate reuse of
  the PR-gate's path layout rather than a re-spelling, and is correct.
- **Module-global state**: the only mutable module-level state in the slice is
  `registry.discovered()`'s `@cache`, which holds manifests (not built halves) and is documented as
  such — verified: `_build_half` is called fresh on every `make_data_source`/`active_*` call.
- **Error taxonomy**: `BindingError`, `PathSyntaxError`, `TransformError`, `WarehouseQueryError`,
  `ElnFormatError`, `OrdFormatError`, `DataSourceError` and `VendoredDatasetError` are each registered
  by exact class name in `durable/publish._BAD_DATA_TYPES`, so Temporal's name-matching
  non-retryability is not silently missing any of them. No caller in the slice string-matches an error
  message.
- **Hardcoded config**: none found. Every threshold in the warehouse engine
  (`fetch_limit`, `max_fields`, `query_timeout_seconds`, `metric`, `top_k`, `embedding_dim`) comes from
  the binding or from `settings`. `snowflake.py` contains no host literal, as its docstring claims.
- **Function size**: nothing in the slice is too large to hold in the head. The two largest,
  `WarehouseElnAdapter.fetch_new_entries` (45 lines) and `IngestBinding._is_coherent` (33), are both
  linear with one concern each.
