# Round 1 — chemclaw3_mock — security & hardening

Repo: `/workspace/chemclaw3_mock`. Slice: `app/` (main, config, hpc/, eln/, mcp_tools/) and `tests/`.
Every finding below was reproduced by running the app; scripts are in `/tmp/repro_mock_sec.py`,
`/tmp/repro_eln_size.py`, `/tmp/repro_eln_append.py` and the inline one-liners quoted in each section.

The headline: the HPC half of this mock has an auth gate that three tests assert, and it is
bypassable three different ways — an undocumented sibling route, a config typo, and an empty
secret. The ELN half has no gate at all, and it deletes and writes files in the directory the
core ingests from.

---

## Unauthenticated `/_mock/reset` recycles workflow ids, so a live handle starts serving a different job's result

- **Severity**: high
- **Location**: `app/hpc/router.py:58-62` (`reset`), with `app/hpc/store.py:110-114` (`JobStore.reset`, `self._sequence = 0`)
- **Trigger**: any anonymous `POST /_mock/reset` — no `Authorization` header — while another caller holds a workflow handle. Every other route on this router carries `dependencies=[Depends(require_launcher_auth)]`; this one carries none, and `include_in_schema=False` keeps it out of `/docs` and the OpenAPI document, so it is invisible to anyone auditing the published API surface. It has **no caller anywhere in the repo** (`grep -rn "_mock/reset"` matches only its own definition — no test, no script, no README): the test fixture calls `job_store.reset()` in-process instead.
- **Consequence**: two distinct failures from one anonymous request. (1) Denial of service: every in-flight handle 404s, and the core's `poll_run` turns a 404 into `NextflowError("poll failed: …")`, failing a durable QM job. (2) Worse, and silent: `reset()` sets `_sequence = 0`, so the very next launch is issued the id `mock-run-000001` again. A caller still holding that id now polls to `SUCCEEDED` and fetches an artifact — someone else's artifact — and parses it as its own result. The `energy=` value the core caches under caller A's molecule is caller B's molecule's energy. Nothing anywhere reports an error.
- **Evidence**: `app/hpc/router.py:58` is the only route in the file with no `dependencies=` argument. Reproduction output:

  ```
  caller A: id=mock-run-000001 artifact='energy=-1716.560765 converged=True'   # smiles=CCO
  unauthenticated POST /_mock/reset -> 200 {"status":"reset"}
  caller A polls its handle again -> 404
  caller B: id=mock-run-000001 artifact='energy=-1867.592612 converged=True'   # smiles=c1ccccc1
  ID REUSED: True  -> A's handle now serves B's benzene energy, not A's ethanol
  ```

- **Fix**: delete the route — it has no caller. If a remote reset is wanted, add `dependencies=[Depends(require_launcher_auth)]` and make ids non-recycling by seeding `_sequence` from a monotonic counter that `reset()` does not touch (or use `uuid4`), so an id can never be reissued to a second job.

---

## A typo'd `MOCK_HPC_ENFORCE_AUTH` value silently turns off all HPC authentication

- **Severity**: high
- **Location**: `app/config.py:21-25` (`_env_bool`), consumed at `app/hpc/auth.py:22` and `:33`
- **Trigger**: set `MOCK_HPC_ENFORCE_AUTH` to anything outside the literal set `{"1","true","yes","on"}` — `enabled`, `True!`, `y`, `enforce`, `1 ` with a stray character, a value that survived a shell quoting mistake. `_env_bool` returns `False` for every one of them. The default when the variable is *absent* is `True`, so the failure mode is "someone tried to be explicit and got the opposite".
- **Consequence**: `require_launcher_auth` and `require_artifact_auth` both `return` immediately at their first line. Launch, poll and artifact fetch all serve anonymous requests. There is no startup log line, no warning, and no test that would catch it — the three auth tests in `tests/test_hpc.py` monkeypatch `hpc_enforce_auth` to `True` directly and never exercise the parser. This fails **open**: an unparseable value should be an error, and a security switch is exactly the setting where "I don't understand this value" must not mean "off".
- **Evidence**:

  ```
  $ MOCK_HPC_ENFORCE_AUTH=enabled uv run python -c '...'
  MOCK_HPC_ENFORCE_AUTH=enabled -> hpc_enforce_auth = False
    POST /workflow/launch with NO Authorization -> 200 {'workflowId': 'mock-run-000001'}
    GET artifact with NO Authorization -> 200 'energy=-1871.054169 converged=True'
  ```

- **Fix**: make `_env_bool` raise on an unrecognized value (`raise ValueError(f"{name}={raw!r} is not a boolean")`) rather than falling through to `False`. Accept the false set explicitly (`0/false/no/off`) and reject everything else. A crash at import is the correct outcome for a misconfigured auth switch.

---

## The ELN control surface has no authentication and deletes JSON files it did not create

- **Severity**: high
- **Location**: `app/eln/router.py:16-48` (no `dependencies=` on any route, no `Depends` imported at all), reaching `app/eln/seed.py:30-33` (`_clear_dir`, `existing.unlink()`)
- **Trigger**: anonymous `POST /eln/reset`. No header, no body.
- **Consequence**: `seed_all(reset=True)` calls `_clear_dir` on `settings.eln_export_dir` and `settings.ord_export_dir` and unlinks **every** `*.json` in them — not only the files the mock wrote. `app/config.py:39-43` and `app/eln/seed.py:3-8` both state these directories are meant to be set to the *same* paths the core is configured with (`CHEMCLAW_ELN_EXPORT_DIR` / `CHEMCLAW_ORD_EXPORT_DIR`), i.e. the directory the core's sync activity ingests from. So an unauthenticated request destroys whatever else lives there. The same surface also offers unauthenticated `POST /eln/{source}/entries` (writes files into that ingestion directory) and unauthenticated `GET /eln/{source}/entries` (dumps the whole corpus). The asymmetry is the tell: the HPC half was given a token seam and the ELN half was given none, on the same process and the same port.
- **Evidence**: `app/eln/router.py` contains no `Depends`. Reproduction output — a file the mock never created, removed by an anonymous POST:

  ```
  unauth GET /eln/ord/entries -> 45 records leaked, no credential
  unauth POST /eln/json/entries -> 201 files now: 33
  unauth POST /eln/reset -> 200
  victim file exists before reset: True
  victim file exists after unauth reset: False
  ```

  (The victim file was `site-notebook-2019.json`, written into the export dir before the reset.)
- **Fix**: two changes, both small. (1) Put the ELN control routes behind a dependency — either reuse `require_launcher_auth` or add a `MOCK_ELN_API_TOKEN` seam of the same shape. (2) Make `_clear_dir` delete only files the seeder is responsible for: track the seeded filenames (or write them under a `seeded/` subdirectory) and unlink from that set, so a reset can never remove a file the mock did not author.

---

## Unauthenticated ELN reads and writes are a 15 MB / 0.75 s-CPU amplifier with no cap on size, count or rate

- **Severity**: medium
- **Location**: `app/eln/router.py:25-27` (`get_entries`), `:42-48` (`get_entry`), `:30-34` (`add_entry`); `app/eln/seed.py:85-93` (`list_entries`), `:96-103` (`_next_timestamp`)
- **Trigger**: at default settings — `MOCK_HTE_MAX_RECORDS_PER_DATASET` unset, which `app/config.py:49` documents as "seeds every real record" — startup materializes 10,011 ORD files. Then any anonymous `GET /eln/ord/entries`, `GET /eln/ord/entries/<anything>`, or `POST /eln/ord/entries`.
- **Consequence**: each of those requests re-reads and JSON-parses the entire directory. Measured: a listing returns a **15.1 MB** body after **0.91 s** of CPU; a *missing*-id lookup that returns a 404 body of 30 bytes still costs **0.75 s** and the same 15 MB of transient allocation; a zero-byte `POST` costs **0.76 s** because `_next_timestamp` re-reads everything to find the newest timestamp, and it also appends a file, so the cost grows monotonically with the number of requests already served. All three handlers are `def`, not `async def`, so FastAPI runs them in the AnyIO threadpool (40 workers by default): 40 concurrent 30-byte requests hold ~600 MB and saturate every core. There is no rate limit, no page size, no `limit`/`offset`, no cap on the number of appends and no bound on total disk. In a four-repo e2e stack this is a single anonymous request away from wedging the shared test environment.
- **Evidence**:

  ```
  hte cap: 0
  startup seed took 2.5s; ord files on disk: 10011
  unauth GET /eln/ord/entries -> 200, 15.1 MB, 10011 records, 0.91s CPU per request
  unauth GET /eln/ord/entries/<miss> -> 404 after 0.75s (full directory re-read per lookup)
  POST /eln/ord/entries (unauth) -> 201 in 0.76s; request body was empty, files 10011 -> 10012
    next append: 0.74s CPU
  ```

- **Fix**: authenticate the surface (previous finding), then bound it: paginate `get_entries` with `limit`/`offset` (default a few hundred); make `get_entry` read `directory / f"{entry_id}.json"` by name after validating the id against `^[A-Za-z0-9_-]+$` instead of scanning; cache the newest timestamp in the store rather than recomputing it from disk on every append; and cap the number of appended live entries.

---

## An empty configured token authenticates a literal empty bearer

- **Severity**: medium
- **Location**: `app/hpc/auth.py:15-29` (`_bearer_token`, `require_launcher_auth`), `app/config.py:33-34`
- **Trigger**: run with `MOCK_HPC_API_TOKEN=""` (an explicitly-set empty value — `os.environ.get` returns `""`, not `None`, so the `"mock-hpc-token"` default does not apply) and `MOCK_HPC_ENFORCE_AUTH` left at its safe default. Then send `Authorization: Bearer ` with an empty token.
- **Consequence**: `_bearer_token` returns `""`, `expected` is `""`, the comparison succeeds, and the request is authenticated. The same applies to `require_artifact_auth`, where `settings.hpc_artifact_store_token or settings.hpc_api_token` also resolves to `""`. So "no secret configured" reads as "auth is on" from the outside (a bare request 401s, which is what a smoke test would check) while the gate is open to a two-word constant. It also diverges from the client on the other side of this boundary: `_auth_headers` in `connectors/qm/hpc/nextflow.py:53` returns `{}` when the token is empty, i.e. the real client sends **no** `Authorization` header at all in this configuration — so the one caller that exists gets 401 while the empty-token attacker gets 200.
- **Evidence**:

  ```
  === 3. empty configured token authenticates a literal empty bearer ===
    no header            -> 401
    'Bearer ' (empty)    -> 200
    'Bearer anything'    -> 401
  ```

- **Fix**: reject an empty expected token outright — in `require_*_auth`, `if not expected: raise HTTPException(500, "auth enforced but no token configured")` — and reject an empty presented token in `_bearer_token` (return `None` when the remainder is empty). Enforcement with no secret must be a configuration error, not a password.

---

## The launcher's only credential is a constant committed to the repo, on a server bound to 0.0.0.0

- **Severity**: low
- **Location**: `app/config.py:33` (`_env_str("MOCK_HPC_API_TOKEN", "mock-hpc-token")`), `start.sh:10` (`MOCK_HPC_API_TOKEN="${MOCK_HPC_API_TOKEN:-mock-hpc-token}"`), `start.sh:24` (`--host 0.0.0.0`), `start-mcp.sh:7` / `app/config.py:52` (`MOCK_MCP_VENDOR_HOST` default `0.0.0.0`)
- **Trigger**: run `./start.sh` without exporting `MOCK_HPC_API_TOKEN` — the documented way to start the server.
- **Consequence**: the gate that `test_launch_requires_auth` and `test_launch_rejects_wrong_token` assert is, at defaults, a public constant, on every interface. Anyone on the network can launch, poll, enumerate the sequential ids `mock-run-000001…N` and read every artifact, or reset the store (finding 1 does not even need the token). The vendor MCP server has no auth seam at all and also binds `0.0.0.0`. The data is synthetic, so the direct loss is small; the real cost is that the mock's auth behaviour stops resembling the real launcher's, and a test suite that "proves auth works" proves nothing about a deployment.
- **Evidence**: `default token in use: mock-hpc-token | enforce: True` printed by the reproduction script with no env set; `start.sh:10` and `start.sh:24` quoted above.
- **Fix**: drop the default — `_env_str` returning `""` and the previous finding's "enforced but unconfigured is a 500" rule together force the operator to set a token or explicitly set `MOCK_HPC_ENFORCE_AUTH=false`. Default both `--host` and `MOCK_MCP_VENDOR_HOST` to `127.0.0.1`, and make `0.0.0.0` the opt-in.

---

## Token comparison is whitespace-tolerant and non-constant-time, so the mock accepts credentials a strict service rejects

- **Severity**: low
- **Location**: `app/hpc/auth.py:15-18` (`_bearer_token`), `:25` and `:37` (`token != expected`)
- **Trigger**: send the token with trailing whitespace or a tab — `Authorization: Bearer mock-hpc-token   ` or `Bearer mock-hpc-token\t`.
- **Consequence**: `authorization[len("bearer "):].strip()` strips whitespace out of the *credential* before comparing, so a malformed header authenticates here and would be rejected by any service that compares the raw credential octets (RFC 6750 defines the token as `b64token`, which contains no whitespace). This is the mock-accepts-what-the-real-service-rejects shape: a client bug that appends a newline from a secret file passes every e2e test and fails against Seqera. Separately, `!=` on a `str` short-circuits at the first differing byte, so the comparison is not constant-time; that is standard practice to fix even where, as here, remote exploitation is not realistic.
- **Evidence**:

  ```
  'Bearer mock-hpc-token'                       -> 200
  'bearer mock-hpc-token'                       -> 200        # case-insensitive scheme: correct per RFC 7235
  'BeArEr    mock-hpc-token   '                 -> 200        # leading + trailing whitespace eaten
  'Bearer mock-hpc-token\t'                     -> 200        # tab eaten
  ```

- **Fix**: split the header once on a single space and compare the remainder verbatim (`scheme, _, token = authorization.partition(" ")`; case-fold only `scheme`), and compare with `hmac.compare_digest(token, expected)`.

---

## The job store grows without bound and stores caller-controlled strings verbatim

- **Severity**: low
- **Location**: `app/hpc/store.py:67-95` (`JobStore.launch`), `app/hpc/models.py:12-29` (`extra="allow"`, no `max_length` on any field)
- **Trigger**: an authenticated caller (or, per finding 2/6, an unauthenticated one) POSTs to `/workflow/launch` repeatedly. `LaunchParams` sets no length limit on `smiles`/`method`/`basis_set` and `extra="allow"` on both models means an arbitrarily large body is parsed before anything is validated.
- **Consequence**: every launch allocates a `Job` that is retained forever — nothing evicts, expires or caps `_jobs`, and `_by_idempotency_key` grows in step with attacker-chosen key strings. The retained `smiles`/`method` are stored verbatim, so a caller controls both the count and the per-entry size. Measured: 500 launches → 500 jobs retained, none evicted. Long-lived e2e stacks leak monotonically.
- **Evidence**:

  ```
  === 5. unbounded job store (auth'd caller) ===
  jobs retained after 500 launches: 500 -> never evicted, no cap
  ```

  Note also that `app/hpc/models.py:5-6` claims the mock "echoes back untouched" everything beyond `params.smiles/method/basis_set`; `LaunchResponse` (`models.py:32-35`) contains only `workflowId` and nothing is echoed. The extras are parsed, retained on the model for the duration of the request, and dropped — the docstring describes behaviour the code does not have.
- **Fix**: add `max_length` to the three `LaunchParams` fields, switch both models to `extra="ignore"` so oversized bodies are not materialized into the model, and bound `JobStore` with an LRU cap (e.g. 10,000 jobs) so a long-running mock cannot be grown without limit.

---

### Out of lens but blocking verification

`tests/test_mcp_vendor.py` cannot be collected as the dependency range stands: `pyproject.toml` pins `mcp>=1.2`, which resolves to `mcp 2.0.0`, where `mcp.server.fastmcp` no longer exists, so `app/mcp_tools/vendor_server.py:15` raises `ModuleNotFoundError` at import. `uv run --extra dev pytest -q` aborts during collection; with that module ignored, 22 tests pass. I reviewed `vendor.py` / `vendor_server.py` by reading rather than by running them, and found nothing through this lens beyond the missing auth seam and `0.0.0.0` bind already recorded above — the catalog is a frozen in-memory list, `search` is a substring match over 20 entries, and neither tool touches the filesystem, the network or a subprocess.
