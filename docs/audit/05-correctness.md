# Phase 5 — Correctness, Error Handling & Resilience Audit

Scope: `chemclaw/`, `agents/`, `workflows/`, `calc/`, `kg/`, `memory/`, `eln/`, `service/`,
`mcp_servers/`, `report/`. No files were modified. Evidence is `file:line`.

**Headline:** this is a carefully-written codebase for the concerns this phase targets. Error
classification, retry policy, subprocess/DB timeouts, and contextvar isolation are largely
deliberate and documented. There are **no** `except: pass`, no `TODO/FIXME/XXX/HACK`, and no
commented-out code blocks anywhere in the source. The findings below are a small number of real
bugs (mostly on the F5 real-Nextflow path and in front-door lifecycle) plus a handful of
lower-severity gaps. Consciously-deferred items are listed separately at the end and are **not**
counted as bugs.

---

## 1. Error propagation

### 1.1 [Medium] Nextflow `response.json()` mis-classified as non-retryable bad data
`workflows/hpc/nextflow.py:86` (`launch_run`) and `:104` (`poll_run`) call `response.json()` after
checking only the HTTP status code. `httpx`'s `response.json()` raises `json.JSONDecodeError`, which
**subclasses `ValueError`**. The QM workflow runs these activities under `BAD_DATA_RETRY`
(`workflows/qm_job.py:47,48,65`), whose `non_retryable_error_types` lists `"ValueError"`
(`workflows/publish.py:32`). Temporal matches by exact class name, and `JSONDecodeError`'s MRO name
that Temporal sees is a `ValueError` — so a launcher that returns **HTTP 200 with a non-JSON body**
(a reverse proxy error page, a truncated/gzip-mangled response, a transient gateway hiccup) makes
the whole QM job fail **permanently** instead of retrying a transient glitch.

- Failure scenario: Tower/Seqera behind a proxy returns `200` + an HTML maintenance page for one
  poll → `poll_run` raises `JSONDecodeError(ValueError)` → activity fails non-retryably → the
  chemist's durable QM job dies even though the run itself is healthy.
- Contrast: genuine transport faults (`httpx.ConnectError`, `httpx.TimeoutException`) are
  `httpx.HTTPError`, correctly **not** in the bad-data list, so those retry. Only the JSON-parse
  path is mis-bucketed.
- Fix direction: wrap `.json()` and re-raise as `NextflowError` (a `RuntimeError`, retryable), the
  same way `drfp_bitstring` normalizes third-party exceptions (`mcp_servers/rxnfp/fingerprint.py:26`).

### 1.2 [Low] `run_turn` echoes the raw exception text to the browser
`service/runner.py:81-83` catches `except Exception` and yields
`ErrorEvent(message=f"The turn could not be completed: {exc}")`. The module docstring promises
"a user-safe message rather than propagating a stack trace"; the stack trace is indeed suppressed,
but `str(exc)` is interpolated verbatim, so an internal exception message (a DSN fragment, an
internal hostname, a KeyError key) can still reach the UI. Correct choice to convert-and-continue;
just consider logging `exc` server-side and sending a generic string to the client. Not a
correctness bug — an info-exposure nuance.

### 1.3 [Informational] The broad catches that exist are all correct
The five `except Exception` sites are each justified and none swallow a root cause silently:
- `agents/audit.py:105` records then **re-raises** (`raise` at `:128`) — observe-only, no swallow.
- `agents/audit.py:159` swallows a *sink* failure by design ("a broken audit store must not fail a
  tool call") and logs it.
- `agents/cli.py:129` keeps the REPL alive across one failed turn and prints the error — intended.
- `service/runner.py:81` — see 1.2.
- `scripts/validate_skills.py:38` and `mcp_servers/rxnfp/fingerprint.py:26` normalize third-party
  parse errors into a reported problem / typed `FingerprintError` — correct.

The bad-data classification design is genuinely good: `ChemclawError(ValueError)`
(`chemclaw/errors.py:15`) unifies reject-and-continue at batch boundaries, `db.connect` deliberately
raises `ConnectionError` (not `ChemclawError`) so unreachable-DB stays retryable
(`chemclaw/db.py:61-62`), and `workflows/publish.py:31-45` enumerates concrete subclass names
because Temporal matches by name not `isinstance` — a subtle point handled correctly.

---

## 2. Race conditions in concurrent / async code

### 2.1 [Medium] Push-back mailbox fetch→mark is not atomic (concurrent-tailer double-delivery)
`agents/session_events.py:80-116` (`stream_new_events`) does a plain `SELECT … WHERE consumed_at IS
NULL` (`:24-27`) then, after yielding, an `UPDATE … SET consumed_at` (`:28`). There is no
`FOR UPDATE SKIP LOCKED` and no single-statement claim. Two concurrent tailers on the same session
(two browser tabs, or two pods if a session were reachable from both) both read the same unconsumed
rows and both yield them before either marks them consumed → the same `job_completed` notification is
delivered twice.

- The single-tailer / restart guarantee the docstring claims **is** upheld (mark happens only after
  all yields complete, so a cancelled consumer causes redelivery, never loss — verified: the `for`
  loop suspends at `yield`, so a `break`/cancel never reaches the `do_mark` at `:113`).
- Mitigation in practice: sessions live in a single process's `app.state.sessions`
  (`service/app.py:67`), and `/sessions/{id}/events` 404s on any pod that doesn't hold the session
  (`service/app.py:127,74-75`), so cross-pod double-tailing mostly can't happen — but same-pod
  concurrent streams for one session still double-deliver. Given the F6 multi-replica OpenShift
  target, claim rows with `UPDATE … WHERE id = ANY(SELECT … FOR UPDATE SKIP LOCKED) RETURNING` if
  exactly-once ever matters.

### 2.2 [Low / documented] Calc-store concurrent-miss double-compute
`calc/store.py:146-177` (`run_cached`) — two concurrent misses on the same key both compute and
last-writer-wins on the upsert. This is explicitly documented (`:160-161`) and consciously deferred
(`DEFERRED.md`: "Per-key in-flight dedup in the calc store"). Benign for deterministic fast
calculators; becomes a cost concern only for real HPC/DFT. **Not a bug** — noted for completeness.

### 2.3 [Informational] Contextvar isolation is correct
`agents/session_context.py` and `agents/identity_context.py` use `ContextVar` with token
set/reset in `run_turn`'s `finally` (`service/runner.py:61-63, 84-87`). Task-local, so concurrent
turns cannot cross session ids or identities. This is the right primitive and is used correctly.

### 2.4 [Informational] Git submitter serialization is correct
`kg/git_submitter.py:25,106` serializes every `submit()` through a module-level `asyncio.Lock`
because `git checkout -B` mutates the whole working tree. In-process safety is sound; cross-process
safety is explicitly delegated to per-process dedicated clones (docstring `:9-15`) — a documented
operational constraint, not a code bug.

---

## 3. External calls — timeout / retry / idempotency

### 3.1 [Medium] Non-idempotent Nextflow launch double-submits on activity retry
`workflows/hpc/nextflow.py:65-89` (`launch_run`) POSTs `/workflow/launch` with no idempotency key.
Temporal activities are at-least-once: if the POST reaches Tower and starts a run but the HTTP
response is lost (network drop, worker crash after send), Temporal retries `submit_to_hpc`
(`workflows/activities.py:46-57`, `retry_policy=BAD_DATA_RETRY`, `activity_max_attempts=5`) and a
**second HPC run** is launched. On the real backend this is duplicate expensive compute.

- The mock path is safe (deterministic inputs-derived id, `workflows/activities.py:57`).
- Fix direction: send a client-supplied idempotency token derived from `qm_job_key(job)` so a
  retried launch reattaches to the existing run rather than starting a new one.
- Note: the live-cluster edge is deferred (`DEFERRED.md` HPC/DFT), but the adapter code itself is
  F5-implemented and tested, so this is a real latent bug in shipped code, not a stub.

### 3.2 [Low] Fingerprint store omits the per-statement timeout that every other store sets
`mcp_servers/fpstore.py:187` calls `db.connect(self._dsn)` **without** `statement_timeout_seconds`,
whereas `calc/postgres_store.py:54-55`, `agents/session_store.py:50-51`,
`agents/session_events.py:50,58-59,73-74`, `agents/audit_store.py:33-34`, and `eln/cursor.py:30-31`
all pass `settings.pg_statement_timeout_seconds`. A slow/degenerate HNSW similarity query or a large
`all_records()` full-table scan (`fpstore.py:203-214`, used by substructure search) therefore has no
per-statement wall-clock bound and can pin the enclosing MCP call / activity for its whole budget.
Likely an oversight given the consistent pattern elsewhere. `connect_timeout` still applies; only the
statement bound is missing.

### 3.3 [Informational] Timeouts and retry bounds are otherwise set from config, not defaulted
- QM workflow: every activity has an explicit `start_to_close_timeout` and `retry_policy`
  (`workflows/qm_job.py:41-72`); the poll budget correctly differs for mock vs. real backend
  (`:54-59`) — a prior review caught a mock-derived cap that would kill real runs.
- BO / ELN / notify / interaction workflows: all pass explicit timeouts + bounded retries
  (`workflows/bo_campaign.py:41-72`, `workflows/eln_sync.py:88-113`, `workflows/notify.py:51-57`,
  `workflows/interaction_approval.py:82-97`).
- HTTP (Nextflow) and LLM calls carry config timeouts and (LLM) a retry budget
  (`workflows/hpc/nextflow.py:57-62`, `agents/llm_provider.py:56-58`, `llm_max_retries=3`).
- Git subprocess is timeout-bounded and kills the child on both timeout and cancellation
  (`kg/git_submitter.py:62-77`) — an unusually careful implementation.
- `bo_activities.evaluate_candidates` (`workflows/bo_activities.py:36-48`) awaits each objective
  serially; that is intended (sequential predicted evaluations), not a missing-timeout gap.

---

## 4. Resource leaks

### 4.1 [Medium] Front-door session maps grow unbounded (no eviction / TTL)
`service/app.py:66-68` initializes `app.state.sessions = {}` and `app.state.session_owners = {}`;
`create_session` (`:99-102`) inserts into both and **nothing ever deletes**. Every conversation
leaves a live `AgentSession` object plus an owner entry for the process lifetime. On a long-running
pod this is monotonic memory growth proportional to total sessions ever created. F3 made *history*
durable in Postgres but the in-memory session/owner maps remain unbounded. No LRU, no idle-TTL, no
removal on stream close. Recommend an eviction policy (idle TTL or capacity LRU).

### 4.2 [Informational] DB connections and MCP tool contexts are correctly context-managed
- Every psycopg connection is `async with await …connect() as conn:` with `commit()` on the write
  paths and implicit close/rollback on exception: `calc/postgres_store.py:60-88`,
  `agents/session_store.py:60-81`, `agents/session_events.py:49-77`, `agents/audit_store.py:33-48`,
  `eln/cursor.py:30-45`, `calc/migrate.py:50-75`, `mcp_servers/fpstore.py:191-227`. If `connect`
  itself raises (`ConnectionError`), there is no connection to leak.
- MCP tool servers are opened per-turn via `AsyncExitStack` and torn down in the same scope
  (`service/runner.py:65-69`), even on exception — the lifecycle the agent constructor delegates to
  its caller is honored.
- Nextflow builds `httpx.AsyncClient` inside `async with` per call (`workflows/hpc/nextflow.py:82,
  100,123`) — closed on every path.

---

## 5. Off-by-one / boundary / None handling

No off-by-one or unchecked-`None` defects were found; the boundary cases that look copy-pasted are in
fact adapted correctly:

- `agents/qm_tools.py:87-105` handles the unknown-workflow-id case (`RPCError → ValueError`) and the
  foreign-workflow case (`ValidationError → ValueError`) explicitly, and reads `handle.result()`
  only when `status == COMPLETED` — no `None`-result deref.
- `service/app.py:133` `pushed.payload.get("job_id", "")` and `:74-75` `_owned_session` `.get()`
  checks are all defaulted/guarded.
- `workflows/hpc/nextflow.py:86,104` chain `.get("workflow", {}).get("status", "")` safely; the only
  issue there is the `.json()` call itself (finding 1.1), not `None` handling.
- `eln/sync.py:71-85` advances `cursor = max(cursor, raw.created_at)` for rejected entries too and
  documents the inclusive-boundary re-fetch as idempotent (`:44-52`) — deliberate, correct.
- `mcp_servers/fpstore.py:36-40` `tanimoto` guards the all-zero `union == 0 → 0.0` case (avoids the
  divide-by-zero / pgvector NaN divergence) — a real edge case handled.
- `kg/note.py:45-66` validates slug edges (`..`, trailing `.`, `.lock`) that the char-class alone
  would miss, because the id becomes a git branch — thorough, not naive.
- `chemclaw/ids.py:35` uses `json.dumps(sort_keys=True, default=str)` for stable hashing. Minor
  latent sharpness: `default=str` on a non-JSON-native object (e.g. a `set` in a params dict) would
  hash its `str()` form, which is deterministic only for the current callers (dicts/scalars/pydantic
  dumps). Not a live bug — no caller passes such a value — but worth knowing if inputs widen.

---

## 6. TODO / FIXME / XXX / commented-out code

**Clean.** A repository-wide search (`TODO|FIXME|XXX|HACK`) over all `.py` files returned **zero**
matches, and no commented-out code blocks were found. Forward-looking work is tracked in
`BACKLOG.md` / `DEFERRED.md` and in docstrings that name the deferral and its reason (e.g.
`agents/audit.py:10-11` "actor … `unknown` until Entra"; `calc/store.py:160-161` concurrent-miss;
`mcp_servers/molfp/search.py:50-52` substructure prefilter), which is the intended pattern per
`CLAUDE.md`. This is a genuine strength, not an absence of debt-tracking.

---

## Consciously-deferred items (NOT bugs — do not action as defects)

Per `DEFERRED.md` / `BACKLOG.md`, these are intentional and correctly stubbed/documented:

- **HPC/DFT live integration** — the mock backend is the CI/local path; real Nextflow is the F5
  seam. (Findings 1.1 and 3.1 are bugs *in the F5 adapter code that ships*, distinct from the
  deferred live-cluster edge.)
- **Per-key in-flight dedup in the calc store** (finding 2.2) — documented benign double-compute.
- **Live Entra / Temporal-broker / OpenShift edges** — real token validation, federation/OBO
  exchange, live cluster durability (`CLAUDE.md` "Live edges remain open", `BACKLOG.md`). Not in
  scope for offline correctness.
- **Audit tamper-evident hash chain** (`agents/audit_store.py:9-10`) — Phase 6.

---

## Priority summary

| # | Sev | Area | File:line | One-line |
|---|-----|------|-----------|----------|
| 1.1 | Medium | Error class | `workflows/hpc/nextflow.py:86,104` | `response.json()` `JSONDecodeError(ValueError)` → non-retryable → permanent QM-job death on a transient 200-but-bad-body |
| 3.1 | Medium | Idempotency | `workflows/hpc/nextflow.py:65-89` | `launch_run` has no idempotency key → activity retry double-launches an HPC run |
| 4.1 | Medium | Resource | `service/app.py:66-102` | `sessions`/`session_owners` dicts never evicted → unbounded memory growth |
| 2.1 | Medium | Race | `agents/session_events.py:80-116` | mailbox fetch→mark not atomic → concurrent tailers double-deliver `job_completed` |
| 3.2 | Low | Timeout | `mcp_servers/fpstore.py:187` | only store that omits `pg_statement_timeout` → unbounded HNSW/scan query can pin the activity |
| 1.2 | Low | Info-exposure | `service/runner.py:83` | raw `str(exc)` reaches the browser in the error event |
