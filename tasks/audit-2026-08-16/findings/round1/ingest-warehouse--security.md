# Round 1 — `ingest/eln/*` + `ingest/sources/*` — security and hardening

Slice: `src/chemclaw/ingest/eln/` (adapter, ord, json_adapter, ord_adapter, warehouse/*) and
`src/chemclaw/ingest/sources/`. All files read in full. Probe scripts under `/tmp/audit/`.

---

## The warehouse ELN retrieve half has no authorization gate, and its binding cannot declare one

- **Severity**: high
- **Location**: `src/chemclaw/ingest/eln/warehouse/retriever.py:81` (`WarehouseVectorRetriever.retrieve`);
  `src/chemclaw/ingest/eln/warehouse/binding.py:435` (`VectorBinding`, `extra="forbid"` at line 444)
- **Trigger**: any turn that reaches `gather_evidence` while `eln-snowflake` (or any warehouse
  source) is in `CHEMCLAW_DATA_SOURCES`. No identity is required — a turn with `get_current_actor()
  is None` is served.
- **Consequence**: the whole warehouse corpus the service credential can see is readable by every
  caller of the chat front door, the CLI and the report workflow, regardless of entitlement. This is
  a confused deputy: the binding connects with one service role (`role: CHEMCLAW_READER` in the
  shipped manifest), so a chemist who could not read project ORION in the ELN itself gets ORION's
  `PROTOCOL_TEXT` back as cited evidence. The sibling source in the same seam does exactly the
  opposite: `ShareDocumentRetriever._entitled()` (`ingest/documents/retriever.py:116`) refuses when
  the caller lacks `required_roles` **and refuses when there is no actor at all**, and
  `sharedrive/datasource.yaml:43` comments that "a caller without the entitlement gets nothing from
  this source — not a filtered list, nothing." Nothing above the retriever compensates:
  `agent/research_tools.py:43` hands every `active_retrieve_sources()` half to
  `retrieval/fanout.sweep_sources`, which reads no identity.
  A deployment cannot even fix this in configuration: `VectorBinding` is `extra="forbid"`, so
  `required_roles:` in the manifest is a load-time error (probe 5 below).
- **Evidence**: `retrieve()` calls `self._chunks(await self._search(...))` with no identity read;
  `grep get_current_roles src/chemclaw/ingest/eln/warehouse/` returns nothing. Run
  (`/tmp/audit/authz_probe.py`, warehouse fake primed with one row):

  ```
  actor on this turn: None roles: set()
  chunks returned to an unauthenticated turn: 1
     eln-snowflake:RX-1 | PROTOCOL_TEXT: confidential project ORION step 3 / PROJECT_CODE: ORION
  ```

  and (`/tmp/audit/probe.py`, case 5):

  ```
  5) required_roles REJECTED: invalid warehouse binding: 1 validation error for WarehouseBinding
  vector.required_roles
    Extra inputs are not permitte...
  ```

- **Fix**: give `VectorBinding` the same `required_roles: list[str]` field
  `DocumentShareBinding` has, and gate `retrieve()` on the identical predicate — empty requirement
  passes, non-empty requires an actor and an intersecting role (`get_current_actor()` /
  `get_current_roles()`). One shared helper for both sources would be better than a second copy of
  the predicate. If a deployment genuinely wants the corpus open, `required_roles: []` says so
  explicitly instead of it being the only possible behaviour.

---

## The `regex` transform is a reachable denial of service on the ingest worker, and the failing row is re-fetched forever

- **Severity**: medium
- **Location**: `src/chemclaw/ingest/eln/warehouse/expr.py:197` (`_regex`), validated by
  `expr.py:312` (`_check_pattern`); reached from `warehouse/adapter.py:288` (`_read`) inside
  `map_to_ord`
- **Trigger**: a binding whose free-text column uses a nested-quantifier pattern (the exact use the
  transform advertises: "Pull one group out of a free-text column"), plus one ELN cell whose text a
  chemist typed. `_check_pattern` compiles the pattern and checks the group index; it does not
  bound backtracking, and no transform has a time or input-length cap.
- **Consequence**: `sync_eln_entries` is an **async** activity (`durable/eln_sync.py:143`), and
  `map_to_ord` is synchronous, so the regex burns CPU on the worker's event loop — stalling the
  heartbeat (`beating(...)`) and every other activity on the `background-jobs` worker. Temporal then
  times the activity out and retries; the row is still at/behind the cursor, so it is fetched again
  and the worker wedges permanently on one cell. Writing that cell requires only ELN write access,
  which is the least privileged actor in this whole data path.
- **Evidence**: `/tmp/audit/redos2.py` — pattern accepted at binding load, then applied to a cell:

  ```
  validate_transform accepted pattern ^(A+)+B
    19 chars ->    0.012s
    21 chars ->    0.048s
    23 chars ->    0.189s
    25 chars ->    0.770s
    27 chars ->    3.093s
  ```

  4x per two characters; a 45-character cell is ~10^5 seconds. Note the contrast inside this
  repository: `core/logging.py:519-596` carries a long, measured argument about exactly this class
  of defect and bounds every one of its own patterns — the binding engine, whose input is *more*
  attacker-controlled, has no equivalent.
- **Fix**: run `apply_transforms` off the loop (`asyncio.to_thread`) *and* bound the input the
  regex sees (e.g. truncate to a configured max cell length before matching), or move to a
  linear-time engine (`re2`) for this one transform. A cap alone is not enough for a wedge that
  re-triggers: also record the row id and reject-and-continue on transform timeout so the cursor can
  advance past a poisoned cell.

---

## `server_embed_function` reaches the SQL text unchecked, contradicting the invariant the package documents and tests

- **Severity**: medium
- **Location**: `src/chemclaw/ingest/eln/warehouse/sql.py:130-135` (`vector_statement`);
  field declared at `binding.py:462` with no `_check_identifier` call in
  `VectorBinding._is_coherent` (`binding.py:490`)
- **Trigger**: a warehouse source using `embedding: server`, with any string in
  `server_embed_function`.
- **Consequence**: the package's stated security property is false as written. `sql.py:5` says
  "Relation and column names reach the statement text, and each one was matched against
  `binding._IDENTIFIER` before it got here"; `binding.py:44` says "everything a binding contributes
  to a statement is either an identifier matching this or a bound parameter"; `warehouse/README.md`
  summarises `sql.py` as "Checked identifiers written, every value bound". `where:` is documented as
  the one deliberate exception — `server_embed_function` is a second, undocumented one. A reviewer
  reading a binding, or an operator trusting `make datasource-validate`, is told this field is
  constrained when it is arbitrary SQL spliced into the middle of the ranking expression of every
  retrieval query. Given `CHEMCLAW_DATA_SOURCES_DIR` is a *mounted* directory (README: "a deployment
  mounts a directory holding its own manifest and never edits this repository"), the manifest is a
  looser trust surface than repo code, and the gap silently widens what a manifest can do.
- **Evidence**: `/tmp/audit/probe.py`, cases 1 and 2 — the checked fields reject a payload, this one
  does not:

  ```
  1) VECTOR SQL: SELECT RXN_ID, PROCEDURE, VECTOR_COSINE_SIMILARITY(EMB, (SELECT SECRET FROM
     ELN.PRIVATE.CREDS LIMIT 1) || IDENT(?)) AS CHEMCLAW_SCORE FROM ELN.PUBLIC.V_RXN
     ORDER BY CHEMCLAW_SCORE DESC LIMIT ?
  1b) ... VECTOR_COSINE_SIMILARITY(EMB, X(?), (SELECT 1) FROM T --(?)) AS CHEMCLAW_SCORE ...
  2) relation: rejected (invalid warehouse binding: ...)
  2) key: rejected (invalid warehouse binding: ...)
  2) vector_column: rejected (invalid warehouse binding: ...)
  ```

- **Fix**: call `_check_identifier(self.server_embed_function, "vector server_embed_function")` in
  `VectorBinding._is_coherent` when `embedding == "server"` — the dotted form
  `SNOWFLAKE.CORTEX.EMBED_TEXT_768` already matches `_IDENTIFIER`, so nothing legitimate is lost. If
  a site genuinely needs an expression there, make it a second documented exception in `sql.py`'s
  module docstring rather than leaving the invariant stated and untrue.

---

## The fetch bound covers only the parent query; child tables and file drops are read without any cap

- **Severity**: medium
- **Location**: `src/chemclaw/ingest/eln/warehouse/sql.py:85` (`related_statement`) and
  `warehouse/adapter.py:131` (`_attach_related`); same class at `json_adapter.py:132` and
  `ord_adapter.py:99`
- **Trigger**: any sync where one batch's entries have a large number of child rows (an analytics
  table with thousands of peaks per reaction, a charge view joined wider than expected), or a
  file-drop directory with many/large `*.json` files.
- **Consequence**: unbounded memory in the ingest worker. `related_statement` emits **no `LIMIT`**
  and `_attach_related` calls `cursor.fetchall()` per block, materialising every child row for up to
  `fetch_limit` (≤5000) entries as Python dicts. `EntryBinding.fetch_limit`'s own docstring claims
  the bound exists to "bound memory on a first sync of a warehouse holding a decade of history" —
  it bounds the parent rows only, and the child fan-out is where the row count actually multiplies.
  The file adapters have no bound at all: they build a list over `sorted(self._dir.glob("*.json"))`
  with `path.read_text()` per file, and `_BoundedIngest.fetch_new_entries`
  (`durable/eln_sync.py:112`) truncates only *after* the inner adapter has returned everything — so
  the "bounded chunk" the activity docstring promises is a bound on PR-gate work, not on the fetch.
- **Evidence**: `/tmp/audit/probe.py`, case 3:

  ```
  3) RELATED SQL: SELECT * FROM ELN.PUBLIC.CHARGES WHERE RXN_ID IN (?, ?) ORDER BY RXN_ID, SEQ ASC
  ```

  Compare case 4, where the entry query does carry `LIMIT ?`. In `_attach_related` there is no
  streaming and no row counter — rows are appended to `owner[block.name]` until the result set ends.
- **Fix**: add a `row_limit` to `RelatedBinding` (default a few thousand) and emit it as a bound
  `LIMIT`, logging when it truncates so an operator sees a partially-mapped reaction rather than an
  OOM; cap the number of files one file-drop fetch materialises (the drop directory is on the same
  trust footing as the warehouse and deserves the same bound).

---

## `connect_options`' redaction promise does not hold for the shape a traceback actually renders

- **Severity**: low
- **Location**: `src/chemclaw/ingest/eln/warehouse/connect.py:56-63` (`connect_options` docstring)
- **Trigger**: any code path that renders the connect kwargs through `repr` — a client echoing its
  configuration in an exception message, or a dict repr in an error string — while
  `private_key_env` names a PEM key.
- **Consequence**: the docstring asserts "Every credential variable is registered with the
  log-redaction inventory *before* it is read, so a driver that echoes its own configuration into a
  traceback cannot put a private key into a log." `redact_secrets` is substring replacement of the
  environment variable's exact value; a PEM is multi-line, and `repr` turns its newlines into `\n`,
  so the registered value no longer occurs in the text and nothing is replaced. The registration is
  real and works for single-line credentials (account, user, password); the claim as written is
  broader than the mechanism.
- **Evidence**: `/tmp/audit/misc_probe.py`:

  ```
  plain multi-line redacted: True
  repr()-shaped redacted: False
  repr output -> connect failed with options={'private_key': '-----BEGIN PRIVATE KEY-----\nMIIEv...
  ```

- **Fix**: convert the PEM to DER in `connect_options` (it is converted anyway in
  `snowflake._private_key_der`) so no PEM string is ever a connect kwarg, or register the value's
  escaped form alongside the raw one in the redaction inventory. Either way, narrow the docstring to
  what the mechanism does.

---

## Checked and found sound (through this lens)

- **SQL construction otherwise**: `entry_statement`, `related_statement` and `vector_statement` bind
  the cursor timestamp, batch keys, query vector/text, filter values and `LIMIT`; every relation and
  column identifier (`relation`, `key`, `created_at`, `modified_at`, `foreign_key`, `order_by`,
  `vector_column`, `content_columns`, `filter_columns`, component/attribute columns) is matched
  against `_IDENTIFIER` at load. Probe 2 confirms rejection. `where:` is literal but is documented as
  such and is manifest-scoped.
- **No dynamic-code surface in transforms**: the vocabulary in `expr.TRANSFORMS` is closed, unknown
  names fail at load, and there is no `eval`, `import` or format-string path. `value_map`, `scale`,
  `clamp`, `default` take data, not callables.
- **Dynamic import** (`sources/registry.resolve_half`, `warehouse/connect._resolve_driver`) is
  driven by manifest strings only — operator trust, same as the deployment's own image — and both
  validate `module:callable` shape and callability before use.
- **Path traversal via an ELN-controlled id**: `retriever._is_merged_note` interpolates a warehouse
  key into a path without validation, but the `reaction-` prefix means the escaping component must
  exist as a directory; measured, `../../outside` and `../outside` both resolve to `is_file=False`
  and the call is a `stat` with no read. The KG note id itself is separately slug-validated
  (`kg/note.py:363`), so a traversal id fails closed at `Note` construction.
- **Snowflake error handling**: `ProgrammingError` really is reduced to errno + query id with the
  statement kept in the pod log, as its comment claims.
- **Vendored dataset**: checksum verification is on by default (`vendored_dataset_verify: bool =
  True`), the manifest is validated before use, and the retriever opens no network path.
- **Manifest validation**: `DataSourceManifest` is `extra="forbid"`, YAML is `safe_load`ed, the
  folder name must match `name:`, and `config` may not shadow `name` — a source cannot rename itself
  into another source's index partition.
