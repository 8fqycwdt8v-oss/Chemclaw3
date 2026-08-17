# chemclaw3_mock — design & simplification review (round 1)

Repo: `/workspace/chemclaw3_mock`, slice `app/` + `tests/`. All measurements below were run with
`/tmp/mockvenv` (`uv pip install -e ".[dev]"`, mcp 1.29.0); baseline suite is **28 passed** in
13.2–18.0 s.

Nothing critical or high through this lens. Eight findings: five that cost real time or force
callers into string-matching, three that are dead weight to delete.

---

## The HTE record cap truncates *after* building all 9,987 records — 6x the test-suite time

- **Severity**: medium
- **Location**: `app/eln/real_hte.py:370-393` (`all_real_hte_records`), consumed at
  `app/eln/seed.py:62-64`; the knob is `app/config.py:47-49`
  (`MOCK_HTE_MAX_RECORDS_PER_DATASET`)
- **Trigger**: any call with a cap set — e.g. the `client` fixture
  (`tests/conftest.py:19`, `hte_max_records_per_dataset = 5`), which every HTTP test uses.
- **Consequence**: `all_real_hte_records` calls all five dataset builders unconditionally and only
  then does `grouped = {k: v[:max_per_dataset] ...}`. Every parse and every record dict is built
  and thrown away: with a cap of 5, **9,987 records are constructed and 9,952 discarded**. The
  comment at `app/config.py:48-49` ("Tests override this to a small number to keep the suite fast")
  is a claim the code only half-honours — the cap avoids the file writes, not the construction,
  which is the larger half.
- **Evidence**:

  ```
  records built with cap=5: 35 in 0.339s
  records built uncapped  : 9987 in 0.355s      # the cap saves 0.016s of build time
  csv parse only: 0.036s                        # so ~0.30s is record construction, all discarded
  seed_all with cap=5 wrote 91 files in 0.797s  # i.e. ~44% of a capped seed is wasted work
  ```

  I applied the fix to a copy at `/tmp/mockcopy` (pass `max_records` down into
  `bh_amination_hte_records` / `suzuki_miyaura_flow_hte_records` /
  `_santanilla_screen_records` / `nielsen_deoxyfluorination_hte_records`, slicing rows before
  building; the BH builder skips a row once its plate counter reaches the cap, preserving the
  per-plate split):

  ```
  /tmp/mockcopy       : 28 passed in 2.55s / 2.26s
  /workspace/…_mock   : 28 passed in 17.98s / 13.19s
  capped output identical: True | bytes: 60936   # json.dumps of all_real_hte_records(max_per_dataset=5), both trees
  ```

  `test_real_hte_datasets_at_full_scale` (the 3955/5760/96/96/80 assertions) still passes on the
  patched tree, so the uncapped path is unchanged.
- **Fix**: push `max_per_dataset` into the five builders as above and drop the trailing slice (keep
  it as a cheap belt-and-braces if desired). Proven behaviour-preserving byte-for-byte on the
  capped path and on the uncapped path by the existing full-scale test.

---

## `get_price` reports "unknown id" in-band, so the caller can only string-match

- **Severity**: medium
- **Location**: `app/mcp_tools/vendor_server.py:41-46` (`get_price`)
- **Trigger**: an MCP client calls `get_price` with a catalog id that is not in `_CATALOG`,
  e.g. `{"catalog_id": "nope"}`.
- **Consequence**: the tool returns a **successful** tool result (`isError: False`, no
  `structuredContent`) whose body is `{"error": "unknown catalog_id 'nope'"}`. Every consumer —
  the agent, and any code between it and the tool — has to detect the failure by probing for an
  `"error"` key or matching the message text; the protocol-level failure signal MCP already
  provides is never used. A mock that answers "not found" with 200-equivalent success trains the
  system on a contract the real vendor tool is unlikely to have.
  Secondary, same site: the return annotations differ (`-> list[dict]` vs `-> dict`), so FastMCP
  publishes an `outputSchema` and `structuredContent` for `search_building_blocks` and **none** for
  `get_price` — one tool is structured, its sibling is text-only, for no stated reason.
- **Evidence** (`/tmp/mcp_probe2.py`, in-memory MCP session against `server._mcp_server`):

  ```
  unknown id -> isError: False | structuredContent: None
     text: {  "error": "unknown catalog_id 'nope'"}
  search -> isError: False | has structuredContent: True
  known  -> isError: False | has structuredContent: False
  ```

  The only test of this path asserts the defect (`tests/test_mcp_vendor.py:38`,
  `assert "error" in missing`).
- **Fix**: annotate both tools with the dataclass (`-> BuildingBlock`, `-> list[BuildingBlock]`)
  and `raise ValueError(f"unknown catalog_id {catalog_id!r}")` instead of returning a dict.
  Verified working (`/tmp/mcp_probe3.py`):

  ```
  get_price_typed outputSchema: {"properties": {"catalog_id": {...}, "name": {...}, …}}
  typed ok:      False {'catalog_id': 'VC-00104', 'name': 'Aniline', …}
  typed missing -> isError: True | Error executing tool get_price_typed: unknown catalog_id 'nope'
  ```

  Not behaviour-preserving — that is the point; `tests/test_mcp_vendor.py:38` changes to assert a
  raise. It also deletes the duplication in the next finding.

---

## Single-entry lookup reads and parses the entire corpus (26 MB, 0.68 s) to return one record

- **Severity**: medium
- **Location**: `app/eln/router.py:42-48` (`get_entry`), via `app/eln/seed.py:85-93`
  (`list_entries`)
- **Trigger**: `GET /eln/ord/entries/<any id>` on a server started with the shipped defaults
  (`start.sh`, `infra/live/e2e-full-stack/up.sh` — neither sets
  `MOCK_HTE_MAX_RECORDS_PER_DATASET`, so the cap is 0 = seed everything).
- **Consequence**: `get_entry` calls `list_entries(...)`, which globs, reads and `json.loads` every
  file in the directory (10,011 files / 26 MB), builds the whole list in memory, then scans it
  linearly — to return a ~2 KB record whose path is already known, because every writer in
  `seed.py` names the file after the id (`f"{record['id']}.json"` line 52/56,
  `f"{record['reactionId']}.json"` line 60/67, and both append helpers, lines 118 and 138). A 404
  costs the same full read. The same whole-corpus read is what makes `POST /eln/ord/entries` (via
  `_next_timestamp`) cost 0.7 s per appended entry.
- **Evidence** (`/tmp/eln_http_probe.py`, `TestClient`, default cap):

  ```
  startup seed: 2.02s
  GET /eln/ord/entries -> 200, 15.1 MB body, 0.89s
  GET /eln/ord/entries/suzuki-flow-hte-05760 -> 200 in 0.68s
  GET  (404 path) -> 404 in 0.65s
  POST /eln/ord/entries -> 201 in 0.72s
  POST /eln/reset -> 200 in 3.01s
  ```

  (`GET /eln/ord/entries` returning a single unpaginated 15 MB array is the same structural choice;
  it at least has to touch every record.)
- **Fix**: in `get_entry`, resolve the file directly —
  `path = directory / f"{entry_id}.json"`, guarded by rejecting any `entry_id` containing a path
  separator or `..` (the guard is required: today's linear scan cannot traverse, a direct join
  can), return its parsed content, 404 if absent. Behaviour-preserving for every id the mock can
  produce, since filename == id for all four writers.

---

## `MOCK_HPC_UNKNOWN_STATUS_EVERY_N` is silently a no-op for every value ≥ 2 at the default poll count

- **Severity**: medium
- **Location**: `app/hpc/store.py:49-59` (`Job.status`), knob declared at `app/config.py:37`
- **Trigger**: run with the documented/default `MOCK_HPC_POLLS_UNTIL_DONE=2` and set
  `MOCK_HPC_UNKNOWN_STATUS_EVERY_N` to 2, 3 or 4 — i.e. the natural reading of "inject an UNKNOWN
  every N polls".
- **Consequence**: the guard is
  `every_n > 0 and poll_count % every_n == 0 and poll_count < polls_until_done`. With
  `polls_until_done=2` the only poll that can satisfy both the modulus and the `<` is poll 1, so
  **only `every_n == 1` ever does anything**; every other value behaves exactly like the "off"
  value 0, with no warning. The knob exists to exercise the real client's `UNKNOWN` handling
  (`chemclaw/connectors/qm/hpc/nextflow.py:42` maps `UNKNOWN → RunState.RUNNING`), so a silently
  inert setting means that path is believed exercised and is not. No test sets it non-zero —
  `tests/conftest.py:18` pins it to 0.
- **Evidence** (`/tmp/hpc_probe.py`, status sequence from repeated `GET /workflow/{id}`):

  ```
  polls_until_done=2 MOCK_HPC_UNKNOWN_STATUS_EVERY_N=0: ['RUNNING', 'SUCCEEDED', …]
  polls_until_done=2 MOCK_HPC_UNKNOWN_STATUS_EVERY_N=1: ['UNKNOWN', 'SUCCEEDED', …]
  polls_until_done=2 MOCK_HPC_UNKNOWN_STATUS_EVERY_N=2: ['RUNNING', 'SUCCEEDED', …]   # identical to off
  polls_until_done=2 MOCK_HPC_UNKNOWN_STATUS_EVERY_N=3: ['RUNNING', 'SUCCEEDED', …]   # identical to off
  polls_until_done=2 MOCK_HPC_UNKNOWN_STATUS_EVERY_N=4: ['RUNNING', 'SUCCEEDED', …]   # identical to off
  ```

- **Fix**: make the interaction explicit rather than emergent. Either drop the
  `poll_count < polls_until_done` term and let `UNKNOWN` replace a `RUNNING` poll only (the
  `poll_count >= polls_until_done` terminal check already comes later if you reorder), or keep the
  semantics and validate at construction: in `Settings.__init__`, raise/clamp when
  `0 < hpc_unknown_status_every_n_polls >= hpc_polls_until_done`. Add one test asserting an
  `UNKNOWN` actually appears. Not behaviour-preserving by design — today's behaviour is the defect.

---

## `SUBMITTED` is unreachable through the status endpoint, contradicting the store's own docstring

- **Severity**: medium
- **Location**: `app/hpc/store.py:1-13` (module docstring), `app/hpc/store.py:52-53`
  (`if self.poll_count == 0: return "SUBMITTED"`), `app/hpc/store.py:101-108` (`JobStore.poll`)
- **Trigger**: launch anything and poll it: `POST /workflow/launch` then `GET /workflow/{id}`.
- **Consequence**: `JobStore.poll` increments `poll_count` **before** the router reads
  `job.status()`, so the first status a client ever sees is `RUNNING` (or `UNKNOWN`). The docstring
  claims "The state machine (SUBMITTED -> RUNNING -> SUCCEEDED/FAILED) advances one step per poll"
  — the `SUBMITTED` step is not on the path. The branch is reachable only through the artifact
  endpoint's 409 detail string (`app/hpc/router.py:50-54`, `job_store.get` does not increment).
  The real client distinguishes `SUBMITTED`/`PENDING` from `RUNNING`
  (`chemclaw/connectors/qm/hpc/nextflow.py:40-41`), so that mapping is never driven by this mock.
  `tests/test_hpc.py:43` hides it behind a disjunction
  (`assert … in ("SUBMITTED", "RUNNING")`), which passes either way.
- **Evidence** (`/tmp/hpc_probe.py`): `SUBMITTED ever returned by GET /workflow/{id}? -> False`
  (6 consecutive polls, `polls_until_done=4`).
- **Fix**: read the status before advancing the clock — have `JobStore.poll` return the status
  computed at the current `poll_count` and *then* increment (or return `(job, status)`), so a
  freshly launched run's first poll answers `SUBMITTED` and the sequence becomes
  `SUBMITTED → RUNNING… → SUCCEEDED`. Tighten `tests/test_hpc.py:43` to `== "SUBMITTED"` and add
  one poll to the lifecycle test. Not behaviour-preserving (it shifts the sequence by one poll,
  which is the point); alternatively delete the `poll_count == 0` branch and the docstring's
  `SUBMITTED` claim — but that removes coverage the real client wants.

---

## Four exact clone sites

- **Severity**: low
- **Location**:
  1. `app/mcp_tools/vendor_server.py:27-36` and `:48-55` — the same 8-key
     `BuildingBlock → dict` literal, written out twice.
  2. `app/hpc/auth.py:21-29` (`require_launcher_auth`) and `:32-41` (`require_artifact_auth`) —
     identical bodies apart from the `expected` expression.
  3. `_iso(dt)` — `app/eln/fixtures_data.py:219-220`, `app/eln/real_hte.py:59-60`,
     `app/eln/real_procedures.py:33-34`, plus the same expression inlined at
     `app/eln/seed.py:117` and `:135-137`. Five copies of
     `dt.isoformat().replace("+00:00", "Z")`.
  4. the ORD role map — `app/eln/real_hte.py:46-51` (`_ROLE`) and
     `app/eln/fixtures_data.py:310-315` (`_ROLE_TO_ORD`): same four keys, same four values, two
     names.
- **Trigger**: no runtime trigger; the cost is that a change to any one of these has three or four
  places to reach and no test that notices when they diverge (e.g. adding a fifth reaction role
  updates one map and silently `KeyError`s in the other builder).
- **Consequence**: maintenance only — 3 of the 4 pairs are byte-equivalent today.
- **Evidence**: `dataclasses.asdict` reproduces both hand-written vendor dicts exactly
  (`/tmp/mcp_probe.py`): `asdict == handwritten: True` for
  `asdict(price("VC-00104")) == get_price("VC-00104") == search_building_blocks("aniline")[0]`.
- **Fix**, all behaviour-preserving: (1) `return asdict(block)` / `[asdict(b) for b in
  search(query)]` — or, better, take the typed-return fix from the `get_price` finding above,
  which deletes both literals; (2) `def _require(authorization, expected)` with the two dependency
  functions reduced to one line each; (3) one `iso(dt)` in a shared `app/eln/_time.py` (or in
  `seed.py`, which already needs it twice), imported by the three data modules; (4) one `_ROLE` map
  in the same shared module.

---

## `seed_all`'s `reset` parameter has no caller that passes `False`

- **Severity**: low
- **Location**: `app/eln/seed.py:36-48`
- **Trigger**: none — grep for callers: `app/main.py:24` (`seed_all(reset=True)`),
  `app/eln/router.py:39` (`seed_all(reset=True)`). No test calls it at all.
- **Consequence**: a parameter, a branch (`else: mkdir(parents=True, exist_ok=True)` twice) and two
  docstring paragraphs exist for a caller that does not exist; the docstring says so out loud —
  "used by the reseed-without-losing-appends case, **if ever needed**". This is exactly the "for
  later" stub the repo's own quality rules say to delete on sight.
- **Evidence**: `grep -rn "seed_all" app tests` →
  `app/main.py:24`, `app/eln/router.py:39`, both `reset=True`.
- **Fix**: delete the parameter and the `else` branch; `_clear_dir` already does
  `mkdir(parents=True, exist_ok=True)`, so the unconditional path is `_clear_dir(...)` for both
  directories. Behaviour-preserving for both live callers.

---

## Every `Job` carries a `threading.Lock` nothing ever acquires

- **Severity**: low
- **Location**: `app/hpc/store.py:33` —
  `lock: threading.Lock = field(default_factory=threading.Lock, repr=False)`
- **Trigger**: none — every launch allocates one.
- **Consequence**: the field reads as "per-job locking exists", which is a claim about concurrency
  safety that the code does not make good on: all mutation goes through `JobStore._lock`
  (`store.py:73`, held in `launch`/`get`/`poll`/`reset`). It also silently makes `Job` unusable
  with `dataclasses.replace`-style copying or equality across processes. A reader auditing the
  poll-count race has to prove the field is unused before trusting `_lock`.
- **Evidence**: `grep -rn "\.lock\b\|lock=" app/ | grep -v "_lock"` → no matches. The only
  identifier hits for `lock` outside this line are `self._lock` in `JobStore`.
- **Fix**: delete the field (and the now-unneeded `field` import if nothing else uses it).
  Behaviour-preserving.
