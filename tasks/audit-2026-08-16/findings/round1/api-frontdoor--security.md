# API front door — security and hardening (round 1)

Slice: `src/chemclaw/api/app.py`, `deps.py`, `middleware.py`, `routes/*.py`.
Everything below was read in full and, where marked, executed. Scripts live under `/tmp/audit/`.

Route/authz inventory was enumerated at runtime (walking every `APIRoute.dependant` tree for
`require_principal`). The result: **every route is gated except `/healthz`, `/readyz`, `/metrics`**,
which matches the stated allowlist. `require_principal` resolves exactly once per request even on
the routes whose tree contains it twice (measured: 1 invocation for
`/sessions/{id}/messages`, `/sessions/{id}/plan`, `/jobs`), so the per-principal rate budget is
spent once, not twice. `openapi_url=None` really does remove the schema route. Those claims hold.

---

## Any authenticated user can enumerate and read every durable job's full result

- **Severity**: high
- **Location**: `src/chemclaw/api/routes/jobs.py:18` (`list_jobs`) and `src/chemclaw/api/routes/jobs.py:38` (`get_job`)
- **Trigger**: Authenticate as any principal (`entra_required=true`, any valid tenant token — no
  role needed). `GET /jobs` with no query parameters. Take any `job_id` from the response and
  `GET /jobs/{job_id}`.
- **Consequence**: A cross-tenant read. `GET /jobs` returns *every* row of `job_records` — job id,
  connector, the free-text `rationale` the requesting chemist wrote, and the run summary — with no
  filter on the caller. `GET /jobs/{job_id}` then returns `DurableJobStatus`, whose `result` is the
  **entire structured payload** the connector job produced (`durable_tools.py:80`,
  `result: dict[str, Any]`). In a multi-team deployment that is another group's optimisation
  output, compound identifiers and conditions, readable by anyone who can get a token. The
  `requested_by` column exists on the row and is never consulted; neither
  `search_job_records(text, connector, limit)` nor `job_status(job_id)` accepts an actor argument
  at all, so no scoping is even expressible without a signature change.
- **Evidence**: The handlers take `principal: CurrentUser` and never read it:

  ```python
  async def list_jobs(principal: CurrentUser, text: str = "", connector: str = "") -> list[JobRecordSummary]:
      return await front_door.search_job_records(text=text, connector=connector)

  async def get_job(job_id: str, principal: CurrentUser) -> DurableJobStatus:
      return await front_door.job_status(job_id)
  ```

  `list_jobs`'s docstring calls the unscoped *listing* deliberate ("`find_past_jobs` … is unscoped
  for the cross-project learning D-004/KM-9 argues for … nothing here pretends the row is
  private"). That argument is about summaries for reuse; `get_job`'s docstring makes **no scoping
  claim of any kind**, and the result payload is the part that is not a summary.

  Reproduced end-to-end (`/tmp/audit/repro_jobs.py`) against the real app with
  `entra_required=true`, Alice's job stubbed on the module's own patch seam and Mallory as the
  authenticated caller:

  ```
  --- Mallory (never touched this job) enumerates GET /jobs ---
  200 [{'job_id': 'job-alice-1', 'connector': 'bo', 'job': 'campaign',
        'rationale': 'Alice asked to optimise the CC-7781 coupling',
        'summary': 'BO campaign for internal candidate CC-7781', ...}]
  --- Mallory reads GET /jobs/job-alice-1 ---
  200 {'job_id': 'job-alice-1', 'status': 'completed',
       'summary': 'BO campaign for internal candidate CC-7781',
       'result': {'best_yield': 0.94, 'conditions': {'solvent': '2-MeTHF', 'T': 55},
                  'compound': 'CC-7781 (unpublished)'},
       'rationale': 'Alice asked to optimise the CC-7781 coupling'}
  ```
- **Fix**: Thread the caller into both reads. `read_job_record_summaries` already selects from a
  table carrying `requested_by`: add an `actor: str` parameter with the self-disabling `(%s = '' OR
  requested_by = %s)` arm `proposal_store._SELECT_MANY` already uses, and have `list_jobs` pass
  `"" if _is_reviewer(principal) else principal.oid` — the exact shape `list_note_proposals`
  already uses one file over. For `get_job`, look the record up first and 404 (not 403) when
  `requested_by` is neither empty nor the caller's oid, matching `_refuse_unless_owner`'s
  no-existence-leak rule. If cross-project reuse is genuinely wanted, keep the *summary* unscoped
  and scope only `result`.

---

## `DELETE /jobs/{job_id}` cancels any Temporal workflow, not just a job

- **Severity**: medium
- **Location**: `src/chemclaw/api/routes/jobs.py:55` (`cancel_durable_job`) → `chemclaw/agent/durable_tools.py:428` (`cancel_job`)
- **Trigger**: Authenticate as a principal holding any role in `entra_privileged_roles` — i.e. the
  *knowledge-proposal reviewer* role, which is the only thing `_is_reviewer` checks. Call
  `DELETE /jobs/note-reindex-202608161955` (the id is `f"note-reindex-{YYYYMMDDHHMM}"`, fully
  derivable from a clock — `durable_tools.py:414-415`) or
  `DELETE /jobs/approval-<interaction_id>` (`interaction_tools.py:33`, derived from the candidate,
  not random).
- **Consequence**: The path segment is handed straight to
  `client.get_workflow_handle(job_id).cancel()` with no check that it names a durable *job*. A
  durable job id is `f"{connector}-{job}-{hash}"` (`connectors/jobs.py:261`); nothing enforces that
  shape. So the reviewer role — granted for "may sign off on machine-written knowledge" — also
  confers "may cancel any workflow in the Temporal namespace": another chemist's pending approval
  hold, the note reindex, a scheduled ELN sync run. The route's own 403 message says
  "cancelling a durable job is an operator action", which is not what the gate actually authorises.
  The read side has the weaker version of the same problem: `get_job` calls `handle.describe()` on
  an arbitrary id and returns `status="running"` for any *running* workflow, making it an existence
  oracle for guessable ids (`completed_job_status` does refuse non-envelope results, so completed
  non-job workflows are not readable — that half is sound).
- **Evidence**:
  ```python
  # routes/jobs.py
  if not _is_reviewer(principal): raise HTTPException(403, ...)
  if not await front_door.cancel_job(job_id): raise HTTPException(404, "no such job")
  # durable_tools.cancel_job
  await client.get_workflow_handle(job_id).cancel()   # no id validation anywhere on this path
  ```
  `_is_reviewer` (`deps.py:81`) is `bool(principal.roles & settings.entra_privileged_role_set)` —
  the identical set used by the proposal-decision route.
- **Fix**: Validate the id before cancelling — look it up in `job_records` (or require the
  `<connector>-<job>-<hash>` shape against the enabled connector set) and 404 anything that is not
  a durable job, so the handle is only ever taken for a workflow this surface owns. Separately,
  consider a distinct entitlement for operator actions rather than reusing the knowledge-review
  role for workflow control.

---

## Security headers are absent on the 413 and on CORS preflight responses

- **Severity**: low
- **Location**: `src/chemclaw/api/middleware.py:192` (`_add_security_headers`) and the install order in `src/chemclaw/api/app.py:202-204`
- **Trigger**: `POST /sessions` with a body larger than `service_max_request_bytes`; or any CORS
  preflight `OPTIONS` when `service_cors_origins` is set.
- **Consequence**: `_add_security_headers`'s docstring states the middleware "sets them on every
  response (including static files and errors)". It does not. `FastAPI.add_middleware` inserts at
  position 0, so the *last* installer is outermost: the built stack is
  `CORS → BodySizeLimit → _SecurityHeaders → router`. Any response manufactured by an outer
  middleware never passes through `_SecurityHeaders.__call__`'s `_send`. The 413 is
  `application/json` served without `X-Content-Type-Options: nosniff`, without CSP and without
  HSTS; the same holds for `ServerErrorMiddleware`'s unhandled-500 page, which is the one response
  most likely to carry a traceback. Small on its own, and a false safety claim in the code.
- **Evidence**: measured with `/tmp/audit/repro_headers.py` (`service_max_request_bytes=1000`,
  `service_cors_origins=https://ui.example.com`):
  ```
  normal route  GET /healthz: HTTP 200            -> all four headers present
  HTTPException GET /notes/...: HTTP 404          -> all four headers present
  oversized body POST /sessions: HTTP 413         -> content-security-policy: <<MISSING>>
                                                     x-content-type-options: <<MISSING>>
                                                     x-frame-options: <<MISSING>>
                                                     strict-transport-security: <<MISSING>>
  CORS preflight OPTIONS /sessions: HTTP 200      -> all four <<MISSING>>
  ```
- **Fix**: Install `_SecurityHeaders` last in `create_app` (so it is outermost), or equivalently
  reorder to `_add_body_size_limit` → `_add_cors` → `_add_security_headers`. Then correct or delete
  the docstring's "every response" sentence if any exclusion remains.

---

## `/readyz` discloses the connector inventory to unauthenticated callers

- **Severity**: low
- **Location**: `src/chemclaw/api/routes/ops.py:91` (`readyz`), body built at `ops.py:121-124`
- **Trigger**: `curl https://<route-host>/readyz` with no credentials.
- **Consequence**: The response body is
  `{"status": ..., "connectors": "qm=reachable, eln=unreachable, ..."}` — the deployment's full
  connector-bundle inventory by name plus each one's live reachability, to anyone who can reach the
  pod. `deploy/helm/chemclaw/templates/service-route.yaml` declares no `spec.path`, so the Route
  exposes this on the external host (the `/metrics` docstring in this same module says as much
  about its sibling). The status word additionally reports whether Postgres is answering. It is
  reconnaissance rather than data — which is why this is low — but it is more than a kubelet needs,
  and the route is deliberately exempt from the per-principal budget, so it is also unmetered.
  Secondary: with `service_readiness_cache_seconds=0` (a documented setting) each unauthenticated
  request becomes an N-connector HTTP fan-out plus a fresh Postgres connection, i.e. a free
  amplifier. The 5 s default is what makes that safe, not the route.
- **Evidence**: `ops.readyz` returns the connector list unconditionally; the auth enumeration above
  confirms `/readyz` carries no `require_principal` node. `_connector_health` and
  `_database_reachable` both run off an unauthenticated request when the cache window has lapsed.
- **Fix**: Return only `{"status": "ready"|"database unreachable"}` from `/readyz` and move the
  per-connector detail behind `CurrentUser` (or onto `/metrics`, which already carries
  `chemclaw_connectors_unhealthy`). Enforce a floor > 0 on `service_readiness_cache_seconds` at
  config validation so the amplifier cannot be configured open.

---

## Checked and found sound (no finding)

Recorded so the triage step does not re-spend the time:

- **Authz coverage.** Every `APIRoute` resolves `require_principal`; the three exceptions are the
  documented probes. Session/approval/proposal gates are all `Depends`-resolved before the handler.
- **All gates fail closed.** `_owner_authorizes` returns `not settings.entra_required` for a
  falsy owner — under enforcement that is *deny*, not allow. `_is_reviewer` denies on an empty
  privileged-role set. `owners.lookup` / `read_proposal` / `approval_owner` raising an
  infrastructure error propagates to 503, never to an open path.
- **SQL.** Every statement reachable from this slice is parameterised —
  `job_record_store._SEARCH`, `tool_results._SELECT_RESULT` / `_SELECT_SESSION_REFS`,
  `proposal_store._SELECT_MANY` / `_SELECT_ONE` / `_MARK_MERGED` (`note_id = ANY(%s)`),
  `session_store._OWNER_*`. No interpolated identifiers or `where:` fragments on this path.
- **Webhook signature.** `_webhook_signature_ok` uses `hmac.compare_digest` and returns False when
  no secret is set; `knowledge_merged` then refuses the consequential half (closing proposals) with
  401 while still allowing the idempotent reindex. `request_note_reindex` is deduped to one
  workflow per calendar minute, so the unsigned path is not a workflow-spam vector.
- **Body limits.** `BodySizeLimit` checks the declared `Content-Length` *and* counts a chunked body
  as it arrives, so the cap is not `Content-Length`-only. `MessageIn._bounded` caps a message at
  `service_max_message_chars`. `KnowledgeMergedIn` parse errors are reported as a count, not as
  materialised pydantic errors.
- **`/metrics` content.** `_COUNTER_LABELS` is a closed declaration and `increment` raises on an
  undeclared label; the only label values reachable are `profile` (validated against the registry
  at session creation), `connector`, `tool`, `source`, `subsystem`, and two literals. No session
  id, oid or turn content can reach the exposition.
- **Error bodies.** `SubsystemUnavailableError`'s "no hostname, port or driver text" claim holds at
  all four raise sites; `_database_unavailable` returns the generic capacity wording and logs the
  DSN-bearing cause server-side only. No bearer token or secret reaches a log line in this slice.
- **CSP.** The policy matches the served UI: `index.html` has exactly one inline `<style>` and one
  external `<script src="/app.js">`, and `app.js` contains no `innerHTML` / `insertAdjacentHTML` /
  `eval` sink. `script-src 'self'` therefore also neuters injected inline handlers.
- **CORS.** Empty allow-list by default; measured — a non-allowed `Origin` gets no
  `Access-Control-Allow-Origin`. `allow_credentials` is not enabled and the app authenticates by
  bearer token only, so a permissive origin list is not by itself a session-riding vector.
- **Unauthenticated boot.** `_refuse_unauthenticated_exposure` fires on the shipped defaults
  (`entra_required=false` + `service_host=0.0.0.0` raises at `create_app`), and
  `CHEMCLAW_SERVICE_HOST` is the same variable `deploy/entrypoint.sh` passes to `uvicorn --host`,
  so the checked value and the bound value cannot diverge. Config validation additionally refuses
  `entra_required` without an audience/tenant.
- **Rate limit / budget defaults.** Off in code, and `deploy/helm/chemclaw/values.yaml` really does
  set `CHEMCLAW_SERVICE_RATE_LIMIT_PER_MINUTE: "120"` and `CHEMCLAW_BUDGET_ENABLED: "true"` — the
  docstrings' "the chart turns it on" claim is true.
- **Turn/stream slot accounting.** `_claim_turn_slot` and the event-stream cap both test-and-set
  with no `await` between the check and the write, so neither has a TOCTOU window on the loop.
- **Attachments.** Filename is reduced by `_safe_name` before any use; parse runs off-loop under a
  process-wide slot cap; the store is bounded per session and globally.
