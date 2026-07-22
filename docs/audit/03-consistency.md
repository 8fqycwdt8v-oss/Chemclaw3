# Phase 3 — Cross-Branch Consistency Audit

Repo: `/home/user/Chemclaw3` · Scope: top-level app modules (tests and `.venv` excluded).
Read-only investigation. Evidence is `file:line`.

## Executive summary

This codebase is **far more consistent than a hastily-merged multi-branch repo usually is** —
most "same problem" concerns already have a single deliberate home: `chemclaw/ids.py` (hashing),
`chemclaw/db.py` (connect), `chemclaw/config.py` (config), `chemclaw/errors.py` (error base),
`mcp_servers/fpstore.py` (fingerprint store/ranking), `calc/store.py` (calc cache),
`memory/similarity.py` (clustering). Several docstrings explicitly narrate a Rule-of-Three
extraction that removed a prior divergence.

The residual inconsistencies are **narrow but real**. In rough priority order:

1. **Two live "no authenticated actor" sentinels** — a half-finished F4 migration left the magic
   string `"unknown"` in three places after config introduced `service_actor_id`. (Medium)
2. **`fpstore` connections skip the per-statement timeout** every other store applies. (Medium — safety)
3. **Duplicated timestamp-parsing logic** across the two ELN adapters. (Low–Medium — DRY)
4. **Two `_list()` helpers with the same name but opposite empty-handling contracts** in `eln/`. (Low–Medium)
5. Cosmetic: two DB cursor idioms; one hardcoded logger name. (Low)

Dimensions 1 (logging), 3 (naming), 4 (config), 5 (datetime), 8 (async) are essentially clean.

---

## 1. Logging — one framework, one setup ✅

Single approach throughout: stdlib `logging`, one idempotent `configure_logging()` driven by
config (`chemclaw/logging.py:17`), and modules do `logging.getLogger(__name__)`. No `structlog`,
`loguru`, or second `basicConfig`. Verified across `agents/audit.py:31`, `calc/store.py:20`,
`eln/sync.py:23`, `eln/json_adapter.py:39`, `workers/*`, `workflows/memory_jobs.py:28`,
`scripts/schedules.py:41`.

`print()` appears only in legitimate CLI/stdout tools — `agents/cli.py`, `calc/migrate.py:81`,
`eln/validate.py`, `kg/validate.py`, `scripts/validate_skills.py`, `evals/harness.py`,
`examples/research_demo.py` — never in library/worker code. (The `mcp_servers/molfp/fingerprint.py:40`
grep hit is a false positive: `GetFingerprint(`.)

**One minor divergence:** `agents/identity/hpc_bridge.py:15` hardcodes the logger name —
`logging.getLogger("chemclaw.hpc_bridge")` — instead of `getLogger(__name__)` like every other
module. Cosmetic; the module's own dotted name would produce a slightly different (arguably more
accurate) logger path.

**Recommendation:** Standardize `hpc_bridge.py` on `getLogger(__name__)`. Otherwise: leave as-is,
this is a model of a single logging story.

## 2. Error handling — consistent, disciplined ✅

One documented convention, well followed:
- **Bad data** derives from `ChemclawError(ValueError)` (`chemclaw/errors.py:15`) so batch
  boundaries catch one type and Temporal treats it as non-retryable. Subclasses:
  `FingerprintError` (`mcp_servers/fpstore.py:22`), `ElnFormatError`/`OrdFormatError`,
  `EvalCaseError`, `NoteError`, etc.
- **Transient infra** is raised as `ConnectionError`, *deliberately not* a `ChemclawError`, so it
  stays retryable (`chemclaw/db.py:9-11,61`).
- Reject-and-continue boundaries catch the domain base: `eln/sync.py:76`, `workflows/memory_jobs.py:45`.

Broad `except Exception` is rare and every occurrence is annotated with a justification:
`agents/audit.py:159` ("a broken audit store must not fail a tool call"), `agents/cli.py:129`
("keep the session alive across a single failed turn"), `service/runner.py:81`,
`mcp_servers/rxnfp/fingerprint.py:26` ("DRFP raises its own NoReactionError; normalize it"),
`scripts/validate_skills.py:38`. **No silent `except: pass`, no bare `except:`.**

**Recommendation:** No change. This is the strongest dimension.

## 3. Naming conventions — layered but coherent (one leftover, see §7-lite below)

The identity concept has three names by design, each documented and layer-appropriate:
`oid` (the Entra claim) → `actor` (the ambient turn identity, `agents/identity_context.py`,
`agents/authz.py`) → `requested_by` (the durable workflow payload field, `workflows/models.py:23`).
This is one value wearing role-specific names, not accidental drift, and is called out in the
docstrings. Acceptable, though it does raise the reader's cost.

- `session_id` — **uniform** (69 uses, zero `sessionId`) across `agents/`, `service/`, `workflows/`.
- `correlation_id` — uniform for the audit/conversation id.
- `job_id` — uniform in app code; `workflowId`/`workflow_id` appear only where Temporal's own API
  dictates the spelling (`agents/qm_tools` describe path, `workflows/qm_job.py:90`). Acceptable.

**The one genuine naming defect is the sentinel value, covered in item §Sentinels below** — the
same "no actor" concept is spelled `"unknown"` in some places and `service_actor_id`/`"service-account"`
in others.

## 4. Config loading — single source ✅

`chemclaw/config.py` (`settings` singleton) is the sole config mechanism; `pydantic-settings`,
`extra="forbid"`, validators for half-configured states. **Only two direct `os.environ` reads in
the whole app, both legitimate and documented:**
- `agents/llm_provider.py:85` reads `ANTHROPIC_API_KEY` — config *intentionally* does not store the
  provider key (`config.py:205-208`).
- `chemclaw/logging.py:46` *sets* `OTEL_EXPORTER_OTLP_ENDPOINT`, bridging the one config value to
  the env var the OTel SDK reads.

No module reads `os.getenv` for its own settings. **Recommendation:** No change.

## 5. Date/time handling — uniformly timezone-aware ✅

- **No `datetime.utcnow()` anywhere. No naive `datetime.now()` anywhere.**
- Python timestamps are UTC-aware: `from datetime import UTC, datetime`, epoch as
  `datetime(1970,1,1,tzinfo=UTC)` (`eln/cursor.py:18`), min as `datetime.min.replace(tzinfo=UTC)`
  (`workflows/memory_jobs.py:39`, `eln/validate.py:75`).
- DB timestamps use SQL `now()` (server-side, tz-aware) consistently: `calc/postgres_store.py:27`,
  `eln/cursor.py:22-23`, `agents/session_events.py:28`.
- Both ELN adapters coerce naive parsed timestamps to UTC identically (see the duplication in item 3
  below — the *behavior* is consistent, the *code* is copied).

**Recommendation:** No change to behavior; only de-duplicate the parse (below).

## 6. Validation — pydantic at typed boundaries, manual at raw-JSON edges ✅

Consistent split: everything that is a typed contract is a pydantic `BaseModel` with `Field`
constraints (`workflows/models.py`, `calc/store.py`, `mcp_servers/fpstore.py`,
`agents/session_events.py`, `chemclaw/config.py`). Raw external JSON (ELN/ORD exports) is
hand-validated in the adapters via small `_list`/`_get`/`_component` helpers that raise the
domain `*FormatError` — appropriate, since the input is untyped and must be rejected per-record.
No structurally-similar input is pydantic-validated in one place and left unchecked in another.
The one wrinkle is the *duplicated/divergent* helper (item 4 below), not a missing-validation gap.

---

## 7. Duplicate / divergent business logic (most dangerous)

Most of the classic duplication candidates are **already unified** — recording this explicitly
because it is the point of the audit:

- **`calc/store.py` vs `calc/postgres_store.py`** — *not* duplicated logic. They are two backends of
  one `ResultStore` Protocol (`calc/store.py:78`); the hit/miss/persist logic lives once in
  `cached_compute`/`run_cached`. Correct design.
- **ID generation `chemclaw/ids.py` vs `memory/ids.py`** — *not* divergent. `memory/ids.py:9`
  delegates to `chemclaw.ids.stable_hash`; the docstring records that four prior near-identical
  hashers (one on weaker SHA-1) were consolidated here.
- **Fingerprints / retrievers** — unified in `mcp_servers/fpstore.py` (one `tanimoto`, one store
  Protocol, two thin domain shims `molfp`/`rxnfp`); clustering unified in `memory/similarity.py`.

### Genuine finding 7a — Duplicated ISO-timestamp parsing across the two ELN adapters (Low–Medium)

`eln/json_adapter.py:266-279` (`_parse_timestamp`) and `eln/ord_adapter.py:332-348` (`_created_at`)
contain byte-for-byte-equivalent core logic:

```python
parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
...
return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
```

The ORD version's docstring even admits it: *"a naive timestamp is read as UTC (see the free-text
adapter for the same rationale)."* Behavior is identical, so this is low-risk **today**, but it is
exactly the "same problem solved twice" shape: if the Z-handling or naive-coercion policy ever
changes, one adapter will silently diverge.

- **More correct:** neither is wrong; both are the intended policy.
- **Recommendation:** Extract one `parse_iso_utc(value: str) -> datetime` helper (e.g. in
  `eln/adapter.py`, which already holds the shared `RawEntry`/`ElnAdapter` contract) and have both
  adapters call it, each wrapping the shared `ValueError` in its own `*FormatError`.

### Genuine finding 7b — Two `_list()` helpers, same name, opposite contracts (Low–Medium)

Both `eln/` adapters define a module-private `_list(payload, key)` — **same name, divergent semantics:**

- `eln/json_adapter.py:212` — **required:** raises `ElnFormatError` if the field is missing *or empty*.
- `eln/ord_adapter.py:371` — **optional:** returns `[]` when absent, raises only on a non-list.

They don't collide (module-private), but a maintainer moving code between the two adapters in the
same package will get a subtly different empty/missing behavior under an identical call. This is the
"divergent solution to the same problem" pattern in miniature.

- **Recommendation:** Rename to intent-revealing names (`_require_list` vs `_optional_list`), ideally
  as two shared helpers in `eln/adapter.py`, so the contract is visible at the call site rather than
  hidden behind an identical name.

### Genuine finding 7c ("Sentinels") — Two live "no authenticated actor" values (Medium)

`chemclaw/config.py:325` explicitly states `service_actor_id` replaced *"the old magic `\"unknown\"`
literal."* The migration is **incomplete** — `"unknown"` is still the live default/sentinel in three
places:

- `workflows/models.py:35` — `requested_by: str = "unknown"`
- `agents/chemclaw_agent.py:89` — `actor: str = "unknown"` (default parameter)
- `agents/audit.py:166` — `audit_tool_calls = make_audit_middleware(..., actor="unknown")`

against the config-driven values `settings.service_actor_id` (`"service-account"`,
`agents/authz.py:69`) and `settings.cli_admin_actor` (`"admin@localhost"`). So an unattributed
QM job or audit event is tagged `"unknown"` in some paths and `"service-account"` in others — two
spellings of the same "no user" concept, and one of them is precisely the magic string the config
comment claims was removed.

- **More correct:** the config-driven `service_actor_id`.
- **Recommendation:** Replace the remaining `"unknown"` defaults with `settings.service_actor_id`
  (or make the field required and populate via `require_actor()` at every construction site, which
  the submit path already does). At minimum, converge on one sentinel.

---

## 8. Async / concurrency — clean ✅

- `asyncio.run` appears **only** at process entrypoints (`workers/hpc_worker.py:63`,
  `workers/background_worker.py:106`, `agents/cli.py:167`, `calc/migrate.py:80`,
  `scripts/schedules.py:114`, `eln/validate.py:75`). **No `asyncio.run` nested inside async code.**
- Blocking CPU/IO work is consistently offloaded with `asyncio.to_thread`: RDKit/BoFire in
  `agents/bo_tools.py:54-55`, `workflows/bo_activities.py:25-33`, the sync calculators in
  `calc/store.py:174`, and the synchronous graph disk-parse in `report/retrievers.py:58` /
  `agents/graph_tools.py:55` (the latter's comment even points at the shared pattern).
- **No sync `psycopg.connect` in an async path** — every DB call uses `psycopg.AsyncConnection` via
  `chemclaw/db.py`. No `nest_asyncio`, no `run_until_complete`.
- Retry/backoff/timeout is centralized in config + Temporal retry policies
  (`workflows.publish.BAD_DATA_RETRY`, `activity_max_attempts`), not hand-rolled per module.

**Recommendation:** No change.

## 9. Database access patterns — one connect helper, raw SQL, two small wrinkles

**Strong baseline:** every Postgres connection in the app funnels through `chemclaw.db.connect`
(`calc/postgres_store.py:54`, `agents/session_store.py:50`, `agents/audit_store.py:33`,
`agents/session_events.py:49`, `eln/cursor.py:30`, `mcp_servers/fpstore.py:187`,
`calc/migrate.py:50`). Access is raw parameterized SQL held in module-level constants (no ORM
anywhere — consistent). Short-lived connection per call is the uniform, deliberate choice (KISS,
documented identically in several stores). Transactions: explicit `await conn.commit()` after writes,
inside the connection context manager. Writes never interpolate user data (the one dynamic-SQL
site, `fpstore` table/width, is guarded by `table.isidentifier()` at `fpstore.py:156`).

### Finding 9a — `fpstore` connections omit the per-statement timeout (Medium, safety)

Every other store passes `statement_timeout_seconds=settings.pg_statement_timeout_seconds` into
`db.connect` so a hung query is cancelled instead of burning the enclosing activity's whole budget
(`calc/postgres_store.py:55`, `agents/session_store.py:51`, `agents/audit_store.py:34`,
`agents/session_events.py:50-59-74`, `eln/cursor.py:31-42`). **`mcp_servers/fpstore.py:187` does
not:**

```python
async def _connect(self) -> psycopg.AsyncConnection[TupleRow]:
    return await db.connect(self._dsn)   # no statement_timeout_seconds
```

So fingerprint similarity/substructure queries — the pgvector HNSW scans, arguably the *most* likely
to run long on a large corpus — are the one DB path with **no per-statement wall-clock bound**. This
is a divergent solution to the "bound a hung query" problem: everyone else opted in, `fpstore` did
not (likely a branch that predated the timeout knob).

- **More correct:** apply the timeout, matching every other store.
- **Recommendation:** Pass `statement_timeout_seconds=settings.pg_statement_timeout_seconds` in
  `fpstore._connect` (migrations remain the intended exception — `calc/migrate.py:50` connects
  without one on purpose, since an index build may run long).

### Finding 9b — Two cursor idioms (Low, cosmetic)

Two styles coexist, sometimes in the same file:
- `async with conn.cursor() as cur: await cur.execute(...)` — `calc/postgres_store.py:61`,
  `agents/session_store.py:61`, `fpstore.py:211`.
- `await conn.execute(...)` directly on the connection — `agents/audit_store.py:36`,
  `agents/session_events.py:52`, `eln/cursor.py:33`, `fpstore.py:192`.

Functionally equivalent in psycopg 3 (the latter opens an implicit cursor). Harmless, but `fpstore`
mixing both within one class is the kind of small stylistic drift that signals a merge seam.

- **Recommendation:** Optional. Pick one idiom (the direct `conn.execute` is terser for
  single-statement calls) and apply it per file for readability. Not a correctness issue.

---

## Consolidated recommendations (priority order)

| # | Item | Files | Severity | Action |
|---|------|-------|----------|--------|
| 1 | Two "no actor" sentinels (`"unknown"` vs `service_actor_id`) | `workflows/models.py:35`, `agents/chemclaw_agent.py:89`, `agents/audit.py:166` | Medium | Converge on `settings.service_actor_id` |
| 2 | `fpstore` skips per-statement timeout | `mcp_servers/fpstore.py:187` | Medium (safety) | Pass `pg_statement_timeout_seconds` |
| 3 | Duplicated ISO-timestamp parse | `eln/json_adapter.py:266`, `eln/ord_adapter.py:332` | Low–Med | Extract `parse_iso_utc` into `eln/adapter.py` |
| 4 | Two `_list()` with opposite empty-handling | `eln/json_adapter.py:212`, `eln/ord_adapter.py:371` | Low–Med | Rename `_require_list`/`_optional_list`, share |
| 5 | Hardcoded logger name | `agents/identity/hpc_bridge.py:15` | Low | Use `getLogger(__name__)` |
| 6 | Two cursor idioms | `fpstore.py`, `audit_store.py`, `session_events.py` | Low | Optional per-file consistency |

**Dimensions with no action needed:** logging setup (2 of 2 concerns clean), error-handling
convention, config source, datetime/timezone handling, validation strategy, async/concurrency.
