# chemclaw3_mock — CORRECTNESS findings (round 1)

Repo: `/workspace/chemclaw3_mock`. Slice: `app/` (main, config, hpc/, eln/, mcp_tools/) and `tests/`.
Real-contract cross-checks were made against `/home/user/Chemclaw3/src/chemclaw/connectors/qm/hpc/nextflow.py`,
`.../ingest/eln/ord_adapter.py` and `.../ingest/eln/json_adapter.py` — the code on the other side of each boundary.

Baseline: the mock's own suite is green (`28 passed`) once `mcp` is pinned below 2.0 (see finding 3).

---

## 5,760 of the 9,987 seeded ORD records — the entire Suzuki-Miyaura dataset — are rejected wholesale by the real ORD adapter

- **Severity**: high
- **Location**: `app/eln/real_hte.py:217-249` (`suzuki_miyaura_flow_hte_records`), specifically the
  `_component(None, "reactant", name=row["r2_name"])` at :222-224 and `product_name=f"Suzuki-Miyaura coupling product of …"` at :238
- **Trigger**: start the mock with the default seed (`MOCK_ELN_SEED_ON_STARTUP=true`), point
  `CHEMCLAW_ORD_EXPORT_DIR` at `MOCK_ORD_EXPORT_DIR`, run the ELN sync.
- **Consequence**: every one of the 5,760 Suzuki records is written to disk, fetched by
  `OrdJsonAdapter.fetch_new_entries`, and then rejected by `map_to_ord` with
  `OrdFormatError: compound has no resolvable structure identifier`. 57.7% of the ORD corpus the mock
  advertises never reaches the knowledge graph. Any E2E assertion about corpus size, dataset coverage,
  or "did the sync ingest the HTE data" measures roughly half of what the mock claims to provide, and
  the loss is per-entry-silent (it lands in the sync report, not as a failure).

  The mock's own tests cannot see this: `tests/test_eln.py:25-43` and `:156-170` assert only that
  *some* identifier type is present (`{"SMILES","NAME"}`), and the comment at `tests/test_eln.py:26-30`
  states that NAME-only identifiers are acceptable "matching real ORD schema flexibility". That is the
  claim, and it is false against this consumer: `ord_adapter._smiles` resolves a `NAME` only through
  `chemclaw.core.reagents.resolve_compound_name`, a committed synonym table that deliberately returns
  `None` rather than guessing. `"2a, Boronic Acid"` and the synthesized product description are not in it.
- **Evidence**:

  ```
  $ .venv/bin/python /tmp/audit_ord_full.py          # every seeded record -> OrdJsonAdapter.map_to_ord
  bh-amination-plate-p2et: 1320/1320 accepted
  bh-amination-plate-btmg: 1317/1317 accepted
  bh-amination-plate-mtbd: 1318/1318 accepted
  suzuki-miyaura-flow-hte: 0/5760 accepted
      x1536  OrdFormatError: compound has no resolvable structure identifier:
             {'identifiers': [{'type': 'NAME', 'value': '2a, Boronic Acid'}], 'reactionRole': 'REACTANT'}
      x1536  OrdFormatError: compound has no resolvable structure identifier:
             {'identifiers': [{'type': 'NAME', 'value': '2b, Boronic Ester'}], 'reactionRole': 'REACTANT'}
  santanilla-amidation-screen: 96/96 accepted
  santanilla-sulfonamidation-screen: 96/96 accepted
  nielsen-deoxyfluorination-screen: 80/80 accepted
  TOTAL: 4227/9987 accepted; 5760 seeded files the ORD adapter rejects
  ```

  and directly:

  ```
  >>> from chemclaw.core.reagents import resolve_compound_name as r
  >>> r("2a, Boronic Acid")
  None
  >>> r("Suzuki-Miyaura coupling product of 6-chloroquinoline with 2a, Boronic Acid")
  None
  ```

  The four distinct `r2_name` values in `app/eln/real_data/suzuki_miyaura_flow_hte.csv` are
  `'2a, Boronic Acid'`, `'2b, Boronic Ester'`, `'2c, Trifluoroborate'`, `'2d, Bromide'` — all
  paper shorthand, none resolvable.
- **Fix**: the four coupling partners are named compounds with known structures (phenylboronic acid,
  its pinacol ester, the potassium trifluoroborate, the aryl bromide) — put their SMILES in a
  four-entry lookup in `real_hte.py` and emit a `SMILES` identifier alongside the real `NAME`, which
  keeps the paper's shorthand as provenance while making the record ingestible. Give the product the
  same treatment (or omit the product name and emit the real coupled-product SMILES). Whichever is
  chosen, add a test that runs a sample of each dataset through `OrdJsonAdapter.map_to_ord` rather
  than through a shape assertion — a shape test cannot see this class of defect at all.

---

## `POST /workflow/launch` accepts a body carrying no chemistry and returns a converged energy

- **Severity**: high
- **Location**: `app/hpc/models.py:12-29` (`LaunchParams` / `LaunchRequest`: `extra="allow"`, every
  field defaulting to `""`), consumed by `app/hpc/router.py:23-30` (`launch`)
- **Trigger**: `POST /workflow/launch` with `{}`, or with the QM params under any key other than
  `smiles`/`method`/`basis_set` (a rename or a dropped field in the caller).
- **Consequence**: the launcher answers `200 {"workflowId": …}`, the run reaches `SUCCEEDED`, and the
  artifact store serves a well-formed `energy=… converged=True` that `parse_qm_output` parses without
  complaint. Because `QMJobResult` is assembled from the *caller's* `job.molecule_smiles`
  (`connectors/qm/activities.py:156-163`), that number is then persisted and cached as the requested
  molecule's energy. A real Seqera/Tower launch with no pipeline and no params is a 400; here every
  malformed launch produces the *same* plausible-but-wrong answer, so two different molecules under a
  wrong key are indistinguishable — which is exactly the regression an E2E test of the launch payload
  exists to catch, and it passes green.
- **Evidence**:

  ```
  $ python /tmp/audit_hpc.py
  launch with EMPTY body -> 200 {'workflowId': 'mock-run-000002'}
    artifact for the paramless run -> energy=-1342.150502 converged=True
  launch with smiles=null -> 422   (only an explicit null is rejected; absence is not)

  $ python /tmp/audit_launch.py     # caller renamed `smiles` -> `molecule`
  ethanol under wrong key -> energy=-336.536339 converged=True
  benzene under wrong key -> energy=-336.536339 converged=True
  identical energy for two different molecules: True
  ```
- **Fix**: make `smiles`, `method` and `basis_set` required (`str = Field(min_length=1)`, no default)
  on `LaunchParams` and `pipeline` required on `LaunchRequest`, so a launch missing any of them is a
  422 the way the real launcher's 400 would be. Keep `extra="allow"` for the fields the mock genuinely
  echoes; it is only the *defaults* that turn a missing required field into a silent success.

---

## The vendor MCP server does not start: `mcp>=1.2` resolves to mcp 2.0.0, which has no `mcp.server.fastmcp`

- **Severity**: high
- **Location**: `pyproject.toml:11` (`"mcp>=1.2"`) and `app/mcp_tools/vendor_server.py:15`
  (`from mcp.server.fastmcp import FastMCP`)
- **Trigger**: a clean install today — `uv venv && uv pip install -e .` — followed by
  `./start-mcp.sh` (i.e. `python -m app.mcp_tools.vendor_server`), or `pytest`.
- **Consequence**: the process dies on import. One of the three things this repo exists to mock — the
  external vendor MCP tool that exercises Chemclaw3's `HttpMcpServerSpec` path — cannot be started at
  all from a fresh checkout, and `pytest` cannot even collect (`tests/test_mcp_vendor.py` errors out,
  taking the whole run with it, so the HPC and ELN suites do not run either).
- **Evidence**:

  ```
  $ uv venv /tmp/freshvenv && uv pip install -e .
  $ /tmp/freshvenv/bin/python -c "import importlib.metadata as m; print(m.version('mcp'))"
  2.0.0
  $ /tmp/freshvenv/bin/python -m app.mcp_tools.vendor_server
    File "/workspace/chemclaw3_mock/app/mcp_tools/vendor_server.py", line 15, in <module>
      from mcp.server.fastmcp import FastMCP
  ModuleNotFoundError: No module named 'mcp.server.fastmcp'

  $ pytest -q                      # same env
  E   ModuleNotFoundError: No module named 'mcp.server.fastmcp'
  ERROR tests/test_mcp_vendor.py
  !!!! Interrupted: 1 error during collection !!!!

  $ uv pip install "mcp<2" && pytest -q
  28 passed
  ```

  (mcp 2.0's `mcp.server` package exposes `mcpserver`, `lowlevel`, `streamable_http`, … — `fastmcp`
  is gone.)
- **Fix**: pin the range the code actually supports — `"mcp>=1.2,<2"` — or port the two tools to the
  2.x server API. Either way the constraint must be bounded; an unbounded `>=` on a library that
  renames its entry point is what turned a supported dependency into a crash.

---

## Concurrent `POST /eln/{source}/entries` silently loses entries and stamps them with an identical timestamp

- **Severity**: medium
- **Location**: `app/eln/seed.py:106-119` (`append_uspto_entry`, the `seq = len(list(...glob("*.json"))) + 1`
  at :114) and `app/eln/seed.py:122-139` (`append_ord_entry`, same pattern at :132); both reached
  through `app/eln/router.py:30-34`
- **Trigger**: two or more overlapping `POST /eln/json/entries` (FastAPI runs these `def` endpoints in
  a threadpool, so overlap is the normal case for a driver that fires appends in parallel). Reproduced
  with 8 concurrent calls into `append_uspto_entry`.
- **Consequence**: two failures at once, both check-then-act on the directory listing.
  (a) `seq` is derived from the file count *before* the write, so concurrent callers compute the same
  `seq`, build the same filename, and one `write_text` overwrites the other — the API returns eight
  `201 Created` responses describing eight entries while only seven exist on disk. The caller is told
  work was created that was destroyed.
  (b) `_next_timestamp` likewise reads the maximum stamp before any of them are written, so every
  appended entry gets the *same* `timestamp`. The docstring says each entry is "stamped after every
  existing entry (tests cursor sync)"; that is true of the pre-existing corpus and false of the batch,
  so the appends carry no order relative to each other — the very property the append endpoint exists
  to exercise.
  The same non-uniqueness bites without concurrency: `POST /eln/reset` restores the file count, so the
  next append re-issues an id (`uspto-live-0033`) that a previous append already used with different
  chemistry and an earlier timestamp — and downstream that id becomes the note id `reaction-<id>`,
  where the second record loses to the already-merged check (see `json_adapter._provenance`'s own
  account of id collisions).
- **Evidence**:

  ```
  $ python /tmp/audit_append.py
  files before: 32
  ids returned by the 8 '201 Created' responses: ['uspto-live-0033', 'uspto-live-0034',
      'uspto-live-0035', 'uspto-live-0036', 'uspto-live-0036', 'uspto-live-0037',
      'uspto-live-0038', 'uspto-live-0039']
  distinct ids returned: 7
  files after: 39 -> entries actually created: 7
  timestamps on disk: ['2024-01-16T17:00:01Z'] x7   (all seven identical)
  ```
- **Fix**: put both appenders behind one module-level `threading.Lock` covering read-count →
  compute-timestamp → write, and stop deriving the id from the file count: keep a monotonic counter
  in the module (reset by `seed_all`) or derive the id from the timestamp, so `reset` cannot re-issue
  a live id. `tests/test_eln.py:53-80` only ever appends once, which is why this is invisible today.

---

## One real free-text record (119.43% yield) is rejected in full by the ELN JSON adapter, contradicting the comment that says it is preserved

- **Severity**: medium
- **Location**: `app/eln/real_procedures.py:264` (`"yield_percent": 119.43` for
  `santanilla-orgsyn-boronate-well-Y36`), with the claim at `:309-315`
- **Trigger**: seed the ELN export dir and run `JsonExportAdapter.map_to_ord` over it — i.e. an
  ordinary ELN sync.
- **Consequence**: `OrdReaction.yield_percent` is `Field(ge=0.0, le=100.0)`
  (`ingest/eln/ord.py:151`), so the whole record is rejected — its reactants, catalyst, base, product
  and the paper's quoted procedure all lost, not just the out-of-range number. 1 of the 32 free-text
  records the mock seeds never lands. The code comment asserts the opposite: the record narrates the
  value as "can exceed 100% due to detector response differences … not clipped here", presenting
  non-clipping as a deliberate fidelity decision. Against this consumer, not clipping deletes the
  record.
- **Evidence**:

  ```
  $ .venv/bin/python /tmp/audit_json_adapter.py
  fetched: 32 of 32 files on disk
    REJECT santanilla-orgsyn-boronate-well-Y36 ElnFormatError
           entry 'santanilla-orgsyn-boronate-well-Y36': cannot map to a reaction:
           1 validation error for OrdReaction / yield_percent / Input should be less than or equal to 100
  json adapter accepted 31 / 32
  ```

  None of the five HTE CSVs contain a yield outside `[0, 100]` (checked: max 100.0, min 0.0), so this
  is the only instance — it is a hand-written constant, not data.
- **Fix**: this is a real published >100% area-ratio yield, so the honest fix is to carry it in a way
  the consumer accepts: keep the 119.43 figure in the procedure prose (it is already narrated there,
  with the explanation) and set the structured `yield_percent` to the capped/normalized value, or drop
  the structured field for that record so the rest of it ingests. If instead the position is that
  >100% must round-trip, that is a change to `OrdReaction` in the core repo and needs to be raised
  there — but the mock must not ship a record it knows the consumer rejects. Add an
  adapter-acceptance test the way finding 1 describes.

---

## `GET /workflow/{id}` can never return `SUBMITTED`, so the real client's SUBMITTED/PENDING branch is never exercised

- **Severity**: low
- **Location**: `app/hpc/store.py:49-59` (`Job.status`, the `if self.poll_count == 0` branch) together
  with `app/hpc/store.py:101-108` (`JobStore.poll` increments *before* the router reads the status)
- **Trigger**: launch any run, then poll it repeatedly.
- **Consequence**: `poll()` advances `poll_count` to 1 before `router.poll` calls `job.status()`, so
  the `poll_count == 0` branch is unreachable over HTTP. The store's module docstring
  (`app/hpc/store.py:6-8`) describes the state machine as "SUBMITTED -> RUNNING -> SUCCEEDED/FAILED"
  and claims Chemclaw3's polling loop is "genuinely exercised"; the `SUBMITTED`/`PENDING` →
  `RunState.SUBMITTED` mapping in `nextflow.py:39-48` is in fact never driven by this mock. A
  regression in that branch of the client passes E2E. The mock's own test hides it by asserting
  `in ("SUBMITTED", "RUNNING")` (`tests/test_hpc.py:43`).
- **Evidence**:

  ```
  $ python /tmp/audit_hpc.py
  statuses over 4 polls (polls_until_done=2): ['RUNNING', 'SUCCEEDED', 'SUCCEEDED', 'SUCCEEDED']
  ```
- **Fix**: read the status *before* incrementing (return the state the run was in when polled, then
  advance), which makes the first poll `SUBMITTED` and turns the documented three-state machine into
  the observed one. Tighten `tests/test_hpc.py:43` to assert `== "SUBMITTED"` so the branch stays
  reachable.

---

## Vendor catalog search lowercases SMILES, so an aliphatic query matches aromatic rings

- **Severity**: low
- **Location**: `app/mcp_tools/vendor.py:60-69` (`search`, `needle in block.smiles.lower()`)
- **Trigger**: `search_building_blocks(query="CC")` — an agent looking up a building block by SMILES
  fragment.
- **Consequence**: SMILES is case-significant (upper `C` = aliphatic carbon, lower `c` = aromatic).
  Folding it to lowercase makes every benzene ring match a query for an aliphatic C–C fragment: 17 of
  the 20 catalogue entries come back for `"CC"`, including Benzoic acid (`O=C(O)c1ccccc1`) and Aniline
  (`Nc1ccccc1`), which contain no C–C single bond between aliphatic carbons at all. The agent is
  handed a priced, in-stock listing for a compound that does not contain the queried fragment.
- **Evidence**:

  ```
  $ python -c "from app.mcp_tools.vendor import search; ..."
  'CC'         17 ['4-Bromoanisole', 'Phenylboronic acid', 'Benzoic acid', 'Aniline', 'Benzaldehyde', 'Bromobenzene']
  'c1ccccc1'    7 ['Phenylboronic acid', 'Benzoic acid', 'Aniline', 'Benzaldehyde', 'Bromobenzene', 'Iodobenzene']
  'Brc1ccccc1'  1 ['Bromobenzene']
  ```
- **Fix**: match the name case-insensitively and the SMILES case-*sensitively* — `needle in block.name.lower()
  or query.strip() in block.smiles`. One line, and it keeps the deliberately-simple substring
  semantics the docstring claims while no longer conflating two different atoms.

---

### Scope note

Checked and found sound: the `Idempotency-Key` header binding (FastAPI's `idempotency_key` parameter
does match the client's `Idempotency-Key`, verified end to end), the launcher/artifact bearer-token
split including the same-origin fallback `_artifact_headers` relies on, the artifact endpoint's
409-before-terminal behaviour, `JobStore`'s locking (`poll_count` is only ever mutated under
`JobStore._lock`; the per-`Job` `lock` field at `store.py:33` is dead but nothing races without it),
the monotonicity of `Job.status` once terminal (so the artifact endpoint's check-then-act cannot
flip), the `energy=… converged=…` artifact format against `parse_qm_output`'s regex, and the
`UNKNOWN`-injection branch (reachable, and mapped to non-terminal `RUNNING` by the real client).
