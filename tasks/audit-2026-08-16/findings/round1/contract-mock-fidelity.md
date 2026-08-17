# Contract audit — mock fidelity (`Chemclaw3_mock` vs. the real clients in `Chemclaw3`)

Scope: `/workspace/chemclaw3_mock` against `src/chemclaw/connectors/qm/hpc/nextflow.py`,
`src/chemclaw/connectors/qm/activities.py`, `src/chemclaw/ingest/eln/`,
`src/chemclaw/ingest/sources/`, `src/chemclaw/connectors/registry.py`.

Method: the mock was started for real (`uvicorn app.main:app --port 8090`, mock's own venv) and
driven by Chemclaw3's *unmodified* client code from Chemclaw3's venv over HTTP; the mock's ELN
record generators were imported into Chemclaw3's interpreter and fed to the real adapters. Every
finding below is a recorded run, not a reading.

15 findings: 0 critical, 4 high, 6 medium, 5 low.

---

## The vendor MCP tool is unreachable from Chemclaw3, and the misconfiguration is silent

**Severity** High

**Location**
- Mock: `/workspace/chemclaw3_mock/app/mcp_tools/vendor_server.py:20-56` (a bare `FastMCP` server, no
  `connector.yaml` anywhere in the repo), and `/workspace/chemclaw3_mock/README.md:66-70`, which
  prescribes `CHEMCLAW_CONNECTOR_URLS='{"mock-vendor":"http://localhost:8091/mcp"}'`.
- Real: `/home/user/Chemclaw3/src/chemclaw/connectors/registry.py:266-267`
  (`return settings.connector_urls.get(connector, endpoint.url)`) and
  `/home/user/Chemclaw3/src/chemclaw/core/config/connectors.py:38-45`.

**Trigger** Wire the mock exactly as its README says and start Chemclaw3.

**Consequence** `connector_urls` is a *per-name override of a discovered bundle's URL*, not a way to
add a connector. `mock-vendor` is not a discovered bundle (there is no `connector.yaml` for it in
either repo), so the entry is never consulted: at runtime nothing raises, nothing logs, and the two
vendor tools simply never appear in the agent's surface. Any end-to-end run "with the external
vendor MCP tool attached" is a false pass — the tool was never attached. The mock's third stand-in
(one of the three named in its own README table) is a no-op.

**Evidence**

```
$ CHEMCLAW_CONNECTOR_URLS='{"mock-vendor":"http://localhost:8091/mcp"}' .venv/bin/python -c ...
connector_urls seen by settings: {'mock-vendor': 'http://localhost:8091/mcp'}
enabled connectors at runtime: ['bo', 'calc', 'chem', 'molfp', 'qm', 'rxnfp', 'safety']
'mock-vendor' reachable: False -> no error raised, silently ignored
```

The one place it *is* loud is the offline validator, which is not on the runtime path:

```
$ CHEMCLAW_CONNECTOR_URLS='{"mock-vendor":...}' python -m chemclaw.cli.validate_connectors
connector validation failed:
  - settings.connector_urls names unknown connector 'mock-vendor'; discovered connectors:
    ['bo', 'calc', 'chem', 'molfp', 'qm', 'rxnfp', 'safety']
```

**Fix** Ship a `manifests/mock-vendor/connector.yaml` in the mock repo, shaped like
`/workspace/chemclaw3-mcp/manifests/props/connector.yaml` — `name`, `description`, `endpoint:
{transport: http, url: http://127.0.0.1:8091/mcp, tools: [...], read_only: [...]}` — and change the
README to say `CHEMCLAW_CONNECTORS_DIR=<mock>/manifests:<shipped>` with `CHEMCLAW_CONNECTOR_URLS`
kept only for moving the address. Separately, Chemclaw3 should log a WARNING (not silence) when a
`connector_urls` key matches no discovered bundle; `validate_connectors` already knows how to say it.

---

## The mock manufactures an idempotency guarantee the real launcher only *might* offer, and its dedup is blind to the request body

**Severity** High

**Location**
- Mock: `/workspace/chemclaw3_mock/app/hpc/store.py:76-95` — `launch()` returns the existing job for
  a reused `Idempotency-Key` **without ever comparing the payload**; `app/hpc/router.py:23-30`.
- Real: `/home/user/Chemclaw3/src/chemclaw/connectors/qm/hpc/nextflow.py:107-114` — the comment
  concedes the guarantee is conditional ("so a launcher **that honors the RFC header** collapses the
  retry"); key from `qm_job_key`, `src/chemclaw/connectors/qm/specs.py:76-83`.

**Trigger** Two launches carrying the same `Idempotency-Key` but different bodies. Reachable from a
plain config change, because `qm_job_key` folds in `hpc_pipeline_version` but **not**
`hpc_pipeline_name`.

**Consequence** Two things a passing e2e cannot distinguish. (a) The mock *always* dedups, so the
COR-2 property "a Temporal retry of `submit_to_hpc` cannot double-submit an expensive DFT run" is
proven by the mock's choice, not by the launcher's — against a launcher that ignores the header the
retry starts a second cluster run and nothing in the suite would notice. (b) Where the header *is*
honored, RFC-9110-style semantics require a **422** for key-reuse-with-a-different-payload; the mock
returns **200 plus the earlier run**, so a request for pipeline B is silently served pipeline A's
energy.

**Evidence** Real `nextflow.launch_run`, same molecule/method/basis, only `hpc_pipeline_name`
changed between the two calls:

```
pipeline A -> scheduler_job_id='mock-run-000008' key 426f8432372dd3ef
pipeline B -> scheduler_job_id='mock-run-000008' key 426f8432372dd3ef
SAME idempotency key for two different pipelines: True
mock returned the SAME run: True
artifact served for pipeline-B request: 'energy=-1528.041025 converged=True'
```

**Fix** In the mock: store the request body beside the key and return `422` when a reused key
arrives with a different one; add a `MOCK_HPC_HONOR_IDEMPOTENCY_KEY=false` mode so the
double-submit path is actually testable. In Chemclaw3: `qm_job_key` should include
`hpc_pipeline_name` for the same reason it already includes `hpc_pipeline_version` — two pipelines
must not share a key.

---

## 57.5% of the mock's ORD corpus (all 5,760 Suzuki records) is rejected by Chemclaw3's real adapter and permanently dropped

**Severity** High

**Location**
- Mock: `/workspace/chemclaw3_mock/app/eln/real_hte.py:224-227` — the second coupling partner is
  emitted as `_component(None, "reactant", name=row["r2_name"])`, i.e. a `NAME`-only identifier
  whose value is the paper's shorthand (`"2a, Boronic Acid"`); `real_hte.py:236` also emits
  `product_smiles=None`.
- Real: `/home/user/Chemclaw3/src/chemclaw/ingest/eln/ord_adapter.py:319-337` — `_smiles()` tries
  `SMILES`, then `INCHI`, then `resolve_compound_name(NAME)`, and raises `OrdFormatError` when none
  resolves.

**Trigger** Seed the mock at its default `MOCK_HTE_MAX_RECORDS_PER_DATASET=0` (full scale) and run
the ELN sync over `MOCK_ORD_EXPORT_DIR`.

**Consequence** The mock advertises ~10,000 structured records; Chemclaw3 ingests 4,251. The
rejection is not retried: `src/chemclaw/ingest/eln/sync.py:63-66` advances the cursor past rejected
entries deliberately ("a rejection is deterministic bad data"), so the 5,760 records are gone after
the first sync, leaving 5,760 WARNING lines. The mock README's claim that "the two ELN fixture sets
were round-tripped through Chemclaw3's real, unmodified adapter code with zero mapping errors" is
false for the bulk datasets, and any e2e assertion about corpus size, retrieval recall or
fingerprint-index coverage is measuring a different corpus than the one seeded.

**Evidence** Mock generators imported into Chemclaw3's interpreter, fed to the real
`OrdJsonAdapter.map_to_ord`:

```
curated-ord                          n=    24  rejected=     0
bh-amination-plate-p2et              n=  1320  rejected=     0
bh-amination-plate-btmg              n=  1317  rejected=     0
bh-amination-plate-mtbd              n=  1318  rejected=     0
suzuki-miyaura-flow-hte              n=  5760  rejected=  5760   compound has no resolvable
                                     structure identifier: {'identifiers': [{'type': 'NAME',
                                     'value': '2a, Boronic Acid'}], 'reactionRole': 'REACTANT'}
santanilla-amidation-screen          n=    96  rejected=     0
santanilla-sulfonamidation-screen    n=    96  rejected=     0
nielsen-deoxyfluorination-screen     n=    80  rejected=     0

TOTAL seeded to ord dir: 10011   rejected by real adapter: 5760  (57.5%)
```

Note this exactly reproduces the "5,761 of 10,011" figure already written into
`ord_adapter._smiles`'s own docstring as the *motivation* for the NAME-resolution branch — the
branch was added and the corpus is still refused, because the NAME is a paper shorthand that
`chemclaw.core.reagents` cannot resolve and never will.

**Fix** Either resolve the shorthand in the mock (`2a`-`2d` map to four named boronic acids; carry
their SMILES) or, if the intent is to exercise the unresolvable path, cut the dataset to a handful
of records and stop advertising 5,760 ingestible reactions. Either way the mock's own test suite
should call Chemclaw3's `map_to_ord` (it currently only checks its own shapes), which is what would
have caught this.

---

## Seeding on startup deletes the Chemclaw3 repo's committed ELN exports and breaks its test suite

**Severity** High

**Location**
- Mock: `/workspace/chemclaw3_mock/app/eln/seed.py:30-34` (`_clear_dir` unlinks every `*.json`),
  called from `seed_all(reset=True)` at `app/main.py:22-24`, on by default
  (`app/config.py:44`, `MOCK_ELN_SEED_ON_STARTUP=True`).
- Real: `/home/user/Chemclaw3/src/chemclaw/core/config/eln.py:23,50` — defaults
  `eln_export_dir="data/eln-exports"`, `ord_export_dir="data/eln-exports/ord"`, which are **checked
  into this repo** and consumed by `tests/test_eln_recipes.py:33`, `tests/test_eln_workflow.py:69`
  and `tests/test_memory_jobs.py`.

**Trigger** Follow the mock README ("point these at the SAME paths") without overriding
`CHEMCLAW_ELN_EXPORT_DIR`, i.e. set `MOCK_ELN_EXPORT_DIR=<Chemclaw3>/data/eln-exports`, then start
`uvicorn`.

**Consequence** The mock's first action is to delete the repository's fixtures. A real ELN export
process appends; it does not truncate the drop directory. Beyond the data loss, `make test` then
fails on tests that read `data/eln-exports/ord/ord-2026-001.json`, and the failure looks like a code
regression.

**Evidence**

```
BEFORE: eln-exports/eln-2026-001.json
        eln-exports/eln-2026-002.json
        eln-exports/ord/ord-2026-001.json
$ MOCK_ELN_EXPORT_DIR=<copy>/eln-exports ... python -c "from app.eln.seed import seed_all; seed_all(reset=True)"
AFTER:  eln-2026-001.json present: NO - DELETED
```

**Fix** Default `MOCK_ELN_SEED_ON_STARTUP` to seeding *additively* (`reset=False`) and make
`_clear_dir` delete only files it wrote (prefix-scoped, or a manifest of seeded names). Keep the
destructive reset behind the explicit `POST /eln/reset`. Change the README to recommend a dedicated
directory rather than Chemclaw3's default.

---

## A free-text fixture carries `yield_percent: 119.43`, which the real schema refuses

**Severity** Medium

**Location**
- Mock: `/workspace/chemclaw3_mock/app/eln/real_procedures.py:264` (`"yield_percent": 119.43`) with
  the deliberate note at `real_procedures.py:308-314` ("can exceed 100% due to detector response
  differences ... not clipped here").
- Real: `/home/user/Chemclaw3/src/chemclaw/ingest/eln/ord.py:151` —
  `yield_percent: float | None = Field(default=None, ge=0.0, le=100.0)`.

**Trigger** Sync `MOCK_ELN_EXPORT_DIR`.

**Consequence** The whole entry is rejected, not just the field — the real chemistry, procedure text
and provenance of that Santanilla well never reach the graph. The mock's own tests pass because they
never construct an `OrdReaction`. Note the *warehouse* binding solves exactly this case explicitly
(`src/chemclaw/ingest/sources/eln-snowflake/datasource.yaml`, `clamp: {min: 0, max: 100}` on
`yield_percent`), so the schema's stance is known and the file-drop adapters simply have no
equivalent knob.

**Evidence**

```
### eln-json: 32 files on disk, 32 fetched
   mapped OK: 31   rejected: 1
FULL ERROR:
 entry 'santanilla-orgsyn-boronate-well-Y36': cannot map to a reaction: 1 validation error for
 OrdReaction
 yield_percent
   Input should be less than or equal to 100 [type=less_than_equal, input_value=119.43]
```

**Fix** Decide which side is wrong and change that one. If a UPLC area-ratio yield above 100 is real
data the system must keep (it is), `OrdReaction.yield_percent` needs a documented ceiling above 100
or a clamp on the file-drop path, matching what the warehouse binding already does — not a fixture
edit that hides the disagreement.

---

## The launcher mock has no failure modes at all — no 5xx, no 429, no slow response, no redirect

**Severity** Medium

**Location**
- Mock: `/workspace/chemclaw3_mock/app/hpc/router.py:20-55` — the only non-200 outcomes reachable
  are 401 (auth), 404 (unknown id), 409 (artifact before SUCCEEDED) and FastAPI's 422. Every handler
  is synchronous and returns immediately.
- Real: `/home/user/Chemclaw3/src/chemclaw/connectors/qm/activities.py:115-141` — `_poll_nextflow`
  exists almost entirely to absorb transient launcher failures, bounded by
  `hpc_poll_max_consecutive_errors` (default 30, `src/chemclaw/core/config/hpc.py`); and
  `nextflow.py:85` sets `timeout=settings.hpc_http_timeout_seconds` (default 30 s).

**Trigger** Any e2e run against the mock.

**Consequence** Three configured behaviours are never executed end-to-end: the consecutive-error
counter and its reset (`activities.py:132`), the `httpx.HTTPError` branch, and
`hpc_http_timeout_seconds`. A regression that inverted the counter, dropped the `continue`, or
made a blip terminal would pass every test. The 24-hour `hpc_run_timeout_seconds` path is likewise
unreachable, because the mock terminates in `MOCK_HPC_POLLS_UNTIL_DONE` polls and there is no mode
in which a run never finishes.

**Evidence** Full probe of the launcher's reachable status space (real client, running mock):
`launch 200`, `poll 404` on an unknown id, `artifact 409` before success, `401` on a bad token —
nothing else. `grep -n "HTTPException\|status_code" app/hpc/router.py` yields only 404 and 409.

**Fix** Add fault-injection env knobs the way the mock already added `FORCE_FAIL`/`NOCONVERGE`:
`MOCK_HPC_POLL_ERROR_EVERY_N` (return 503), `MOCK_HPC_POLL_DELAY_SECONDS` (exceed the client
timeout), `MOCK_HPC_NEVER_FINISHES`, `MOCK_HPC_LAUNCH_429`. Each is a few lines in `store.status()`
/ the router and turns four dead branches into tested ones.

---

## The artifact store is always same-origin and always a 200 body, so the three-secret model and every real store behaviour go untested

**Severity** Medium

**Location**
- Mock: `/workspace/chemclaw3_mock/app/hpc/router.py:45` — `/artifacts/{id}/qm_output.txt` is served
  by the *same* FastAPI app as `/workflow/launch`; `app/hpc/auth.py:32-41` accepts the launcher
  token as a fallback.
- Real: `/home/user/Chemclaw3/src/chemclaw/connectors/qm/hpc/nextflow.py:65-77` (`_artifact_headers`)
  and `:142-165` (`fetch_artifacts`, a *separate* client precisely because the store may be a
  different origin).

**Trigger** Any e2e run: the mock cannot be configured cross-origin, since it is one process.

**Consequence** `_artifact_headers`'s two other branches are unreachable — the dedicated
`hpc_artifact_store_token` branch and, more importantly, the **cross-origin-with-no-token** branch
that returns `{}`. That last one is the production default for an object store, and the security
property it encodes ("the Seqera credential is never handed to a third host") has no end-to-end
test. Nor does the commonest real artifact-store response: a **302 to a presigned URL**. `httpx`
defaults to `follow_redirects=False` (verified: `httpx.AsyncClient().follow_redirects` is `False`),
so `fetch_artifacts` would see `302 != 200` and raise `NextflowError("artifact fetch failed")` on a
run that succeeded — a failure mode the mock structurally cannot produce.

**Evidence** `follow_redirects default: False` (measured on the installed httpx). Mock artifact route
and launcher route share `app.include_router` in `app/main.py:40-41`; `_same_origin(
"http://localhost:8090/artifacts", "http://localhost:8090")` is `True`, so the launcher token always
rides along in every mock run.

**Fix** Mount the artifact store as a second app on a second port in `start.sh`
(`MOCK_HPC_ARTIFACT_PORT`), with its own token and a `MOCK_HPC_ARTIFACT_REDIRECT=true` mode. Then
decide, in `nextflow.fetch_artifacts`, whether `follow_redirects=True` is intended — currently it is
not set either way.

---

## The single-line artifact hides that `parse_qm_output`'s regexes are unanchored and read the *first* match

**Severity** Medium

**Location**
- Mock: `/workspace/chemclaw3_mock/app/hpc/store.py:61-64` — `qm_output_text()` returns exactly one
  line, `energy=… converged=…`, and nothing else.
- Real: `/home/user/Chemclaw3/src/chemclaw/connectors/qm/activities.py:45-46,152-154` —
  `_ENERGY_RE.search(raw_output)` and `_CONVERGED_RE.search(raw_output)`, matched **independently**
  and taking the first occurrence of each.

**Trigger** An artifact containing more than one `energy=` — i.e. any real QM/Nextflow log with SCF
iteration lines.

**Consequence** The parser silently returns the *first* (unconverged) energy and the *first*
`converged=` flag, which need not come from the same iteration. Because the mock's artifact is one
line, no test can distinguish a correct parser from this one, and no test constrains the pipeline's
output format beyond "contains the tokens somewhere".

**Evidence** Real `parse_qm_output`, two inputs:

```
mock artifact                    -> energy=-155.041025 converged=True
realistic multi-iteration log    -> energy=-154.1      converged=False
```

(the multi-iteration log's *final* line is `energy=-155.041025 converged=True`.)

**Fix** Anchor the contract: have the mock emit a multi-line artifact whose last line is the result
(and a `MOCK_HPC_ARTIFACT_STYLE=verbose` mode), and change `_ENERGY_RE`/`_CONVERGED_RE` to match a
single final record — e.g. one regex capturing both fields from the same line, taking the last
match. `QMJobResult.total_energy_hartree` has no bound (`annotation=float required=True`), so
nothing downstream catches the wrong number either.

---

## The launch body is entirely unvalidated: a run with no molecule, no pipeline and no revision succeeds and returns an energy

**Severity** Medium

**Location**
- Mock: `/workspace/chemclaw3_mock/app/hpc/models.py:12-29` — `LaunchParams` and `LaunchRequest`
  both `extra="allow"`, every field defaulting to `""`; `app/hpc/router.py:23-30` reads only
  `params.*` and ignores `pipeline` and `revision` completely.
- Real: `/home/user/Chemclaw3/src/chemclaw/connectors/qm/hpc/nextflow.py:98-106` sends
  `pipeline`/`revision` from `settings.hpc_pipeline_name` / `hpc_pipeline_version`, both of which
  `Settings._hpc_launch_config` (`src/chemclaw/core/config/hpc.py`) *refuses to leave empty* under
  `nextflow` — the version because it enters the cache key.

**Trigger** `POST /workflow/launch` with `{}`, or with a pipeline name/revision that does not exist.

**Consequence** The mock cannot fail a misconfigured deployment. A wrong `CHEMCLAW_HPC_PIPELINE_NAME`
or a revision that was never published produces a happy run and a plausible energy that is then
persisted into the D-011 calculation cache under a key derived from the *intended* pipeline. A real
launcher 404s or 400s on an unknown pipeline/revision, which is the single most likely production
misconfiguration and the one the mock is blind to.

**Evidence**

```
== B. pipeline name/version are NOT validated by the mock ==
launch with bogus pipeline+revision -> scheduler_job_id='mock-run-000002' (accepted)

== C. raw POST with an empty/absent params body ==
  body={}                                        -> 200 {"workflowId":"mock-run-000003"}
  body={"pipeline":"","revision":"","params":{}} -> 200 {"workflowId":"mock-run-000004"}
```
Both of those jobs then serve a well-formed `energy=… converged=True` artifact for the empty
molecule.

**Fix** Make `smiles`, `method`, `basis_set`, `pipeline` and `revision` required (`...`, not `""`)
and reject an unknown `(pipeline, revision)` pair with 404 against a small configured allow-list
(`MOCK_HPC_PIPELINES=qm-pipeline@mock-1`). Drop `extra="allow"` on `LaunchRequest` so a field the
client renames is caught rather than swallowed.

---

## Neither ELN source ever emits an amendment timestamp, so the entire re-ingest-on-correction path is dead

**Severity** Medium

**Location**
- Mock: `/workspace/chemclaw3_mock/app/eln/fixtures_data.py`, `app/eln/real_procedures.py`,
  `app/eln/real_hte.py` — no record carries `modified` (free text) or
  `provenance.record_modified` (ORD); `app/eln/seed.py:96-139` writes all files in one pass, so file
  mtimes never post-date the sync cursor either.
- Real: `/home/user/Chemclaw3/src/chemclaw/ingest/eln/json_adapter.py:141,372-379`
  (`_optional_timestamp`, whose docstring says a present-but-unparseable value must raise) and
  `/home/user/Chemclaw3/src/chemclaw/ingest/eln/ord_adapter.py:435-456` (`_modified_at`, which
  exists because "the record is amended in place and `record_created` does not move"), plus
  `entry_window` / `is_late_arrival` / `warn_late_arrivals` in `ingest/eln/adapter.py`.

**Trigger** Any e2e sync.

**Consequence** Amendment re-fetch, the late-arrival aggregation, the overlap-window replay
(`eln_sync_overlap_seconds`, 86 400 s) and the future-timestamp guard
(`eln_sync_future_tolerance_seconds`) are all untested end-to-end. A correction a chemist makes to a
record after it was ingested is the *only* reason `modified_at` exists, and no fixture in the mock
produces one. `POST /eln/{source}/entries` is documented as exercising the incremental cursor, but
it only appends a brand-new id — never an amendment to an existing one.

**Evidence** Measured over every generator in the mock:

```
eln-json curated   n=  25 {'has impurities': 1, 'has purity': 24, 'outcome!=success': 1}
eln-json real      n=   7 {}                       # 'has modified': 0
eln-ord            n=  45 {'has workups': 24, 'has conditions.temperature': 27,
                           'has PURITY measurement': 24}   # 'has recordModified': 0
```
(`has hypothesis` was also 0 across all free-text records, so `OrdReaction.hypothesis` / D-162 is
likewise never populated by the mock.)

**Fix** Add `POST /eln/{source}/entries/{entry_id}/amend`, which rewrites an existing file with a
`modified` / `provenance.recordModified[]` stamp newer than the cursor, and seed two records that
already carry one. Also add one deliberately corrupt file (truncated JSON, bad timestamp) so the
skip-and-continue branch in both adapters is exercised.

---

## The poll endpoint can never return `SUBMITTED`, `PENDING` or `CANCELLED`

**Severity** Low

**Location**
- Mock: `/workspace/chemclaw3_mock/app/hpc/store.py:101-108` — `poll()` increments `poll_count`
  *before* `status()` reads it, so the `poll_count == 0` → `"SUBMITTED"` branch at `store.py:52` is
  unreachable through `GET /workflow/{id}`; there is no cancel endpoint and nothing emits
  `"CANCELLED"`. `MOCK_HPC_UNKNOWN_STATUS_EVERY_N` defaults to `0` (`app/config.py:37`), so
  `"UNKNOWN"` is off by default too.
- Real: `/home/user/Chemclaw3/src/chemclaw/connectors/qm/hpc/nextflow.py:39-48` maps
  `SUBMITTED`, `PENDING`, `UNKNOWN`, `RUNNING`, `SUCCEEDED`, `COMPLETED`, `FAILED`, `CANCELLED`.

**Trigger** Any poll loop against the mock.

**Consequence** Five of the eight entries in `_STATE_BY_LAUNCHER_STATUS` are never produced. A
user-cancelled Tower run — the second-commonest terminal state after FAILED — has no e2e coverage,
and `RunState.SUBMITTED` is only ever observed indirectly, in the 409 body of the artifact route.

**Evidence**

```
== A. happy path ==            poll 1 -> RUNNING   (never SUBMITTED)
== F. artifact before SUCCEEDED ==
   NextflowError artifact fetch failed: 409 Conflict: {"detail":"run is SUBMITTED, no artifact yet"}
```

**Fix** Read `status()` before incrementing (so the first poll is `SUBMITTED`), default
`MOCK_HPC_UNKNOWN_STATUS_EVERY_N` to a non-zero value, and add a `CANCEL_ME` sentinel alongside
`FORCE_FAIL`/`NOCONVERGE`.

---

## The mock is *stricter* than the client on artifact auth and on a token-less nextflow config

**Severity** Low

**Location**
- Mock: `/workspace/chemclaw3_mock/app/hpc/auth.py:32-41` — `expected = artifact_store_token or
  api_token`, with `MOCK_HPC_ARTIFACT_STORE_TOKEN` defaulting to `""` (`app/config.py:34`);
  `hpc_enforce_auth` defaults to `True` (`app/config.py:35`).
- Real: `/home/user/Chemclaw3/src/chemclaw/connectors/qm/hpc/nextflow.py:51-53,73-77`, and
  `Settings._hpc_launch_config` in `src/chemclaw/core/config/hpc.py`, which requires
  `hpc_api_base_url`, `hpc_pipeline_name`, `hpc_pipeline_version`, `hpc_artifact_store_url` — and
  **not** `hpc_api_token`.

**Trigger** Two supported Chemclaw3 postures: (a) `hpc_launch_interface=nextflow` with no token
(the config validator accepts it; the docstring says the token arrives via a mounted secret);
(b) the three-secret posture where `hpc_artifact_store_token` is set to something *different* from
the launcher token.

**Consequence** Both are refused with 401 by the mock unless the operator separately sets
`MOCK_HPC_ENFORCE_AUTH=false` / `MOCK_HPC_ARTIFACT_STORE_TOKEN`. The stack looks broken for a reason
that would not exist against a launcher fronted by network-level auth or an unauthenticated
in-cluster artifact store.

**Evidence**

```
config accepted nextflow with empty token: nextflow ''
  -> NextflowError launch failed: 401 Unauthorized: {"detail":"missing or invalid Authorization ..."}
  artifact with separate token -> NextflowError artifact fetch failed: 401 Unauthorized
```

**Fix** Either mirror the client's contract (accept an unauthenticated call when
`MOCK_HPC_API_TOKEN` is empty, exactly as `_auth_headers()` sends nothing when the setting is empty),
or have Chemclaw3's `_hpc_launch_config` require `hpc_api_token` under `nextflow` so the two sides
agree. The pairing note in the mock README's env table is not a substitute for either.

---

## The vendor MCP server validates neither the bearer credential nor the turn identity Chemclaw3 stamps on every connector call

**Severity** Low

**Location**
- Mock: `/workspace/chemclaw3_mock/app/mcp_tools/vendor_server.py:20` — `FastMCP("mock-vendor", ...)`
  with no auth middleware and no header inspection.
- Real: `/home/user/Chemclaw3/src/chemclaw/connectors/identity.py:56-77,105-128` — every connector
  call carries `X-Chemclaw-Actor`, `-Roles`, `-Session`, `-Correlation-Id`, `-Dry-Run` plus
  `traceparent`, and `auth_for()` attaches the manifest's bearer token. Compare
  `/workspace/chemclaw3-mcp/manifests/props/connector.yaml`, where every shipped bundle declares
  `auth: {mode: bearer, token_env: ...}`.

**Trigger** Any tool call against the mock vendor server (once finding 1 is fixed and it is
reachable at all).

**Consequence** The stand-in for an *external vendor* tool is the one connector in the system that
demands nothing. A break in `auth_for` or `turn_identity_hook` — a dropped actor header, a token
that stops being sent — produces identical results against this mock, so the identity propagation
the whole connector seam exists for is unverifiable here. Rate limiting, the failure mode an
external vendor API most reliably has, is likewise absent.

**Fix** Require `Authorization: Bearer $MOCK_MCP_VENDOR_TOKEN` and reject a call missing
`X-Chemclaw-Actor`; add `MOCK_MCP_VENDOR_RATE_LIMIT` returning a 429 after N calls.

---

## Two of the four real data sources — the Snowflake ELN and the mounted share — have no stand-in at all

**Severity** Low

**Location**
- Mock: no module addresses either. `app/eln/` writes flat files only.
- Real: `/home/user/Chemclaw3/src/chemclaw/ingest/sources/eln-snowflake/datasource.yaml` +
  `src/chemclaw/ingest/eln/warehouse/` (a driver Protocol, SQL generation, `fetch_limit: 500`,
  `query_timeout_seconds: 60`, `WarehouseQueryError` vs. `ConnectionError`), and
  `src/chemclaw/ingest/sources/sharedrive/datasource.yaml` +
  `src/chemclaw/ingest/documents/`.

**Trigger** Enabling either source in an e2e run.

**Consequence** The only ELN shape the mock models is the one with no service behind it. Everything
that distinguishes a real ELN — connection auth, a query timeout, `fetch_limit` paging against
`eln_sync_batch_size` (the adapter itself warns at
`ingest/eln/warehouse/adapter.py:72-81` when the two are mis-ordered), a partial result set, an
unreachable warehouse — is exercised only by Chemclaw3's own in-repo fake driver, never end to end.
The share's `required_roles` entitlement gate has the same gap.

**Fix** Add a `MOCK_WAREHOUSE_DSN`-style stand-in implementing the `Warehouse` /
`WarehouseCursor` Protocols over the same seeded records, with knobs for slow queries, a mid-page
disconnect and an auth rejection — the Protocol is deliberately tiny (two methods) and this is
cheap. The share can be a seeded directory tree matching the `sharedrive` binding.

---

## Unauthenticated destructive control endpoints and an unbounded listing on the ELN control surface

**Severity** Low

**Location**
- Mock: `/workspace/chemclaw3_mock/app/hpc/router.py:58-62` (`POST /_mock/reset`, no
  `Depends(require_launcher_auth)` — unlike every other route in that file);
  `/workspace/chemclaw3_mock/app/eln/router.py:30-39` (`POST /{source}/entries`, `POST /reset`, both
  unauthenticated); `app/eln/router.py:25-27` — `GET /{source}/entries` returns
  `list_entries(...)` in full, with no `limit`/`offset`.
- Real: n/a — no Chemclaw3 client calls these; they are the mock's own control plane.

**Trigger** Any process that can reach port 8090.

**Consequence** Mostly a mock-hygiene issue, but two concrete effects: `POST /_mock/reset` wipes the
job store mid-run (every subsequent poll 404s until `hpc_poll_max_consecutive_errors` is burned),
and `GET /eln/ord/entries` serializes all 10,011 seeded records into one response — at full-scale
seeding that is a multi-hundred-MB body, which is enough to make the control surface unusable
precisely when the corpus is realistic. Neither mirrors anything the real services do.

**Evidence** `app/hpc/router.py:58` carries no `dependencies=[...]`, while lines 21, 36 and 45 all
do. `app/eln/seed.py:85-93` (`list_entries`) reads and parses every file in the directory on each
call, and `_next_timestamp` (`seed.py:96-103`) calls it again on every append.

**Fix** Put `Depends(require_launcher_auth)` on `/_mock/reset` and an equivalent on the `/eln`
mutating routes; add `limit`/`offset` to `GET /{source}/entries` and cap the default page.

---

## What is genuinely consistent

Worth stating plainly, because these were checked and hold:

- **The three launcher wire shapes match exactly.** `POST /workflow/launch` →
  `{"workflowId": str}`, `GET /workflow/{id}` → `{"workflow": {"status": str}}`, and
  `GET /artifacts/{id}/qm_output.txt` → `energy=<float> converged=<True|False>` are consumed without
  error by the unmodified `nextflow.launch_run` / `poll_run` / `fetch_artifacts` and by
  `parse_qm_output`. The artifact URL the client builds
  (`{hpc_artifact_store_url}/{id}/qm_output.txt`) lands on the mock's route given the README's
  `CHEMCLAW_HPC_ARTIFACT_STORE_URL=http://localhost:8090/artifacts`.
- **The `FORCE_FAIL` sentinel drives the real non-retryable path correctly** — `poll_run` returns
  `RunState.FAILED` and `_poll_nextflow` raises `ApplicationError(type="NextflowRunFailed",
  non_retryable=True)`.
- **The 404 / 409 error bodies are consumed correctly** by `core.http.error_detail` and become
  `NextflowError` with a readable message.
- **The four bulk HTE datasets other than Suzuki map cleanly**: 2,811 of 2,811 Buchwald-Hartwig,
  Santanilla and Nielsen records pass `OrdJsonAdapter.map_to_ord`, including camelCase key handling,
  `additionOrder` ordering, `conditions.temperature` unit conversion, `workups[]` → step kinds and
  `measurements[]` YIELD/PURITY extraction.
- **31 of 32 free-text records map cleanly**, including the impurity-profile record and the
  `outcome: failure` + `failure_reason` record, and the regex temperature/time recovery from
  patent-style prose.
- **`entry_id` namespaces do not collide** between the two sources (measured: empty intersection),
  so the `reaction-<id>` note-id collision the `_provenance` docstring warns about does not occur
  with this fixture set.
