# Replit Integration Issues

Issues discovered during full end-to-end deployment and testing of the Chemclaw3 stack on
Replit (dev mode, `claude-haiku-4-5`, in-memory Temporal, mock HPC + ELN + MCP vendor).
Each section names the target repo. Tested on 2026-07-25/26.

---

## Repo: Chemclaw3 (backend)

### ISSUE-B-1: `CHEMCLAW_NOTE_REPO_DIR` default `"."` is always wrong — ELN sync fails on first run

**Severity:** Blocker (ELN sync cannot complete without this set)

**Symptom:**

```
GitSubmitError: note_repo_dir '.' resolves to /…/services/chemclaw
— the checkout this process is running from.
Submissions reset/clean the working tree and would destroy uncommitted work there.
Set CHEMCLAW_NOTE_REPO_DIR to a dedicated clone of the knowledge repo.
```

The `ElnSyncWorkflow` Temporal activity fails on every attempt (all retries exhaust) because the
default value `"."` always resolves to the service's own checkout directory, which the git submitter
correctly refuses.

**Root cause:** `chemclaw/config.py` line ~872: `note_repo_dir: str = "."`. No deployment runbook
or README flags this as a required variable.

**Fix:** Mark `CHEMCLAW_NOTE_REPO_DIR` as required with no default (or raise a clear
`ConfigurationError` at startup when it equals the process's own git root). Update `README.md` and
`docs/` with the setup steps:

1. `git init /path/to/chemclaw-notes-repo && git commit --allow-empty -m "init"`
2. Add a bare remote and push: `git remote add origin /path/to/chemclaw-notes-remote.git && git push -u origin main`
3. `export CHEMCLAW_NOTE_REPO_DIR=/path/to/chemclaw-notes-repo`

**Workaround applied on Replit:** Created a fresh `git init` repo at
`services/chemclaw-notes-repo`, added a local bare repo as origin, and set
`CHEMCLAW_NOTE_REPO_DIR` to that path.

---

### ISSUE-B-2: `GraphRetriever` resolves `knowledge_dir` relative to service CWD, not `note_repo_dir`

**Severity:** Blocker (knowledge graph search always returns empty without a workaround)

**Symptom:** After a successful `ElnSyncWorkflow` run (49 entries ingested, branches created),
`gather_evidence` and `find_notes` return no results.

**Root cause:** `report/retrievers.py` — `GraphRetriever.__init__` does:

```python
self._dir = Path(notes_dir if notes_dir is not None else settings.knowledge_dir)
```

`settings.knowledge_dir` defaults to `"knowledge"` (relative). It resolves relative to the
**process CWD** (`services/chemclaw/`), not relative to `settings.note_repo_dir`. The notes are
written to `<note_repo_dir>/knowledge/` by the git submitter, but the retriever looks in
`<cwd>/knowledge/`, which is an empty placeholder directory.

Additionally, `knowledge_dir` is validated as a *relative* path (absolute paths are rejected), so
`CHEMCLAW_KNOWLEDGE_DIR` cannot be set to an absolute path without triggering a `ValueError`.

**Fix options:**
- Resolve `knowledge_dir` relative to `note_repo_dir` in the `GraphRetriever` constructor:
  `self._dir = Path(settings.note_repo_dir) / settings.knowledge_dir`
- Or expose a separate `CHEMCLAW_KNOWLEDGE_ABS_DIR` config field that accepts an absolute path
  and is used only by retrievers (not the PR gate).

**Workaround applied on Replit:** Created a symlink:
`services/chemclaw/knowledge → services/chemclaw-notes-repo/knowledge`

---

### ISSUE-B-3: ELN sync PR gate writes to branches; dev docs don't explain how to make notes searchable

**Severity:** High (invisible data — sync "succeeds" but knowledge graph stays empty for new deployments)

**Symptom:** `ElnSyncWorkflow` logs `ingested=49 rejected=0`, Temporal shows `Completed`, but
`gather_evidence` still returns empty. There is no warning or error.

**Root cause:** `kg/git_submitter.py` pushes each note to a `note/<id>` branch and returns
without merging. The `GraphRetriever` only reads `main`. In production, a human reviews and merges
via PR. In dev/CI there is no guidance on how to make the data available for retrieval.

**Fix:** Add a dev/CI runbook section explaining:
1. That ingested notes land on `note/*` branches and must be merged to `main` to become searchable.
2. A one-liner for batch-merging in dev:
   ```bash
   cd $CHEMCLAW_NOTE_REPO_DIR && git checkout main
   git branch | grep "note/" | tr -d ' *' | xargs -I{} git merge --no-ff --no-edit {}
   git push origin main
   ```
3. Consider a `CHEMCLAW_PR_GATE_AUTO_MERGE=true` flag for dev mode that merges to `main`
   directly instead of creating a branch.

---

### ISSUE-B-4: `GET /sessions` and `GET /sessions/{id}/messages` endpoints missing

**Severity:** Medium (sidebar history and transcript reload broken in UI)

**Symptom:** The BFF (`Chemclaw3_ui`) whitelists these routes and forwards them to the FastAPI
backend, which returns `404` / `405`. Only `POST /sessions` and `POST /sessions/{id}/messages`
exist.

**Impact:** The conversation sidebar cannot be populated from the server; the client falls back to
`localStorage`. Reloading the page loses the chat transcript.

**Fix:** Add to `service/app.py`:
- `GET /sessions` — list sessions for the authenticated principal, requires
  `CHEMCLAW_SESSION_STORE=postgres`.
- `GET /sessions/{session_id}/messages` — return the stored transcript for a session.

---

### ISSUE-B-5: `/approvals` REST surface missing — approval workflow cannot be completed from UI

**Severity:** High (Bayesian optimisation and any approval-gated skill are unusable from the browser)

**Symptom:** The agent emits `approval_request` SSE events correctly and the UI renders the
Approve / Reject buttons. Clicking either button fires `POST /api/approvals/{id}/decision` which
the BFF forwards to `POST /approvals/{id}/decision` on the backend — returning `404`.

**Affected BFF routes (all 404 on backend):**
- `GET /approvals` — list pending holds
- `GET /approvals/{hold_id}` — describe one hold
- `POST /approvals/{hold_id}/decision` — approve or reject

The `InteractionApprovalWorkflow` Temporal workflow exists and is registered on the background
worker, but there is no HTTP surface to signal it.

**Fix:** Implement the three endpoints in `service/app.py`. The decision endpoint should call
`temporal_client.get_workflow_handle(hold_id).signal(...)` (or equivalent) to unblock the
waiting workflow.

---

### ISSUE-B-6: Bare azide anion `[N-]=[N+]=[N-]` not caught by structural hazard screener

**Severity:** Medium (safety-critical gap)

**Symptom:** Calling `screen_hazards` with SMILES `[N-]=[N+]=[N-]` (sodium azide / bare azide
anion) returns no hazard flag. The model text correctly notes azides are shock-sensitive, but the
SMARTS rule table does not match the ionic form.

**Confirmed working:** `CC(=O)OOC(C)=O` (diacetyl peroxide) correctly flagged HIGH SEVERITY.

**Expected:** HIGH SEVERITY for `[N-]=[N+]=[N-]`, `[N+]#[N-]` and related linear azide patterns.

**Note:** Organic azides (`C–N3`) may already be covered by a `[N;X1]=[N+]=[N-]` SMARTS attached
to carbon; only the free ionic form was tested here.

---

### ISSUE-B-7: pKa predictions systematically shifted ~+1.5 units vs. experiment

**Severity:** Low / known limitation (model accuracy)

**Symptom:** Predicted pKa values for acetic acid (~6.5) and benzoic acid (~5.6) are ~1.5 units
higher than experimental values (4.76 and 4.20 respectively). The relative ordering is correct.

**Note:** This may be an expected GFN2-xTB solvation accuracy limitation. Worth documenting as a
known systematic offset so users don't treat the absolute values as reliable.

---

### ISSUE-B-8: `artifacts/api-server` path `/api` intercepts all BFF proxy traffic *(fixed in Replit build)*

**Severity:** Blocker (no API calls reach the Chemclaw BFF — every session create and message POST returns 404)

**Symptom:** Every request to `/api/*` (sessions, messages, approvals, healthz) returns 404 from
the wrong server. The Chemclaw BFF never receives any request and logs no activity. "unknown
session" errors appear on every message send, including the first one.

**Root cause:** The pre-existing monorepo `artifacts/api-server` artifact had `paths = ["/api"]`
in its `artifact.toml`. Replit's path router gives the more-specific `/api` match priority over
the Chemclaw UI's catch-all `/`. Every `/api/*` request therefore went to port 8080
(the pre-existing API server) instead of port 19432 (the Chemclaw BFF). The BFF's proxy to the
FastAPI backend was never reached. Even the session recovery path failed because `POST /sessions`
itself hit the wrong server.

**Fix applied on Replit:** Changed `artifacts/api-server` `paths` from `["/api"]` to
`["/api-server"]` (and `previewPath` to `/api-server`). Replit then routes all `/api/*` traffic
to the Chemclaw UI BFF, which proxies correctly to `http://127.0.0.1:8000`.

**Upstream fix:** Document in the Replit deployment guide that the `artifacts/api-server`
monorepo artifact must be moved off `/api` before the Chemclaw UI artifact is registered, or
disabled entirely if only the Chemclaw stack is needed.

---

### ISSUE-B-9: `X-Frame-Options: DENY` + CSP `frame-ancestors 'none'` blocks Replit preview iframe *(fixed in Replit build)*

**Severity:** Blocker for Replit dev (the built-in preview pane is an iframe — with these headers
the browser refuses to render the page there at all)

**Symptom:** The Replit built-in preview shows a blank page or a "refused to connect" error.
The external browser URL works but the in-editor preview does not. All "unknown session" errors
observed in the built-in browser were downstream of the page not loading in the iframe.

**Root cause:** The BFF (`server/index.ts` + `server/config.ts`) sets two iframe-blocking
directives unconditionally regardless of auth mode:
- `X-Frame-Options: DENY` on all static assets (including `index.html`)
- `Content-Security-Policy: frame-ancestors 'none'`

Both are correct for a production deployment behind Entra auth, but block any iframe host
including Replit's own proxy.

**Fix applied on Replit:** Gated both directives on `authMode !== 'dev'`:
- `x-frame-options` header omitted entirely in dev mode
- `frame-ancestors *` in dev mode CSP (allows any embedding origin)

**Upstream fix:** Add a `REPLIT_DEV=true` or `ALLOW_FRAMING=true` env var that relaxes both
directives for sandboxed dev environments, leaving production (`msal` auth mode) unchanged.

---

### ISSUE-B-10: Interrupted turn leaves `tool_use` without `tool_result` in history → next turn crashes with Anthropic 400

**Severity:** High (any client disconnect mid-tool-call permanently poisons the session)

**Symptom:** After the agent emits an approval prompt and the connection is interrupted (user
navigates, tab goes background, or connection drops), the next message to the same session
returns: `The turn could not be completed due to an internal error`.

**Backend log:**
```
anthropic.BadRequestError: messages.6: `tool_use` ids were found without `tool_result` blocks
immediately after: toolu_vrtx_013a5LhG5zZEnTkZRoGawjLE. Each `tool_use` block must have a
corresponding `tool_result` block in the next message.
```

**Root cause:** `runner.py` `run_turn` catches `GeneratorExit` (client disconnect) but the
agent framework has already appended the `tool_use` block to the session's message history
without the corresponding `tool_result`. The partial history is committed. The next `POST
/sessions/{id}/messages` replays the corrupt history to Anthropic → 400.

**Fix:** On `GeneratorExit` / client disconnect, the session history must be rolled back to the
last clean state (before the interrupted turn's `tool_use` block). The agent framework's
`create_session` likely exposes a rollback or checkpoint API; if not, the runner must snapshot
the history length before each turn and truncate on interrupt.

---

### ISSUE-B-11: `reset_job_sink` raises `ValueError` on `GeneratorExit` — ContextVar token cross-context

**Severity:** Medium (noisy; masks the real error in logs and may leave job tracking in a dirty state)

**Symptom:** Every client disconnect during a turn that started a job logs:

```
ValueError: <Token var=<ContextVar name='chemclaw_started_jobs'> at 0x...> was created in a
different Context
```

**Root cause:** `runner.py` line ~139 calls `reset_job_sink(job_sink_token)` in the `finally`
block. When `GeneratorExit` fires, the coroutine's `contextvars.Context` is different from the
one that called `set_job_sink`, so `_started_jobs.reset(token)` raises. The `Token` was captured
in the request context but is being reset in the generator's teardown context.

**Fix:** Guard with `try/except ValueError: pass` in `reset_job_sink`, or store the context
explicitly with `contextvars.copy_context()` and run the reset inside that context:
`ctx.run(reset_job_sink, job_sink_token)`.

---

## Repo: Chemclaw3_ui

### ISSUE-U-1: `happy-dom@^16.0.0` blocked by Replit package security policy

**Severity:** Medium (blocks all unit tests on Replit)

**Symptom:**

```
npm error 403 Forbidden
GET http://package-firewall.replit.local/npm/happy-dom/-/happy-dom-16.8.1.tgz
Blocked by Security Policy
```

Replit's package firewall blocks `happy-dom@16.8.1` (resolved from `^16.0.0`). Because npm
resolves the full dependency tree before downloading, this blocks installation of all
devDependencies including `vite` and `@vitejs/plugin-react`.

**Workaround applied on Replit:** Removed `happy-dom` and `vitest` from `package.json`
devDependencies. The build and BFF server work, but `npm test` is broken.

**Fix options:**
1. Pin `happy-dom` to `^15.x` (or whichever version is not CVE-flagged).
2. Replace `happy-dom` with `jsdom` as the vitest JSDOM environment.
3. Add an `overrides` entry to force a patched version.

---

### ISSUE-U-2: `GET /api/sessions` and `GET /api/sessions/{id}/messages` in BFF whitelist but missing from backend

*(See also ISSUE-B-4 above — fix is in the backend)*

**Symptom:** BFF `server/routes.ts` whitelists `GET /sessions` and `GET /sessions/{id}/messages`
and forwards them to the Chemclaw3 backend, which returns `404`. The sidebar conversation list is
local-only and transcripts are lost on reload.

---

### ISSUE-U-2b: `session_not_found` banner shows no recovery action — user is left stuck *(fixed in Replit dev build)*

**Symptom (screenshot 2026-07-26):** After an API restart, the browser's `localStorage` holds a
stale `sessionId`. `POST /sessions/{stale_id}/messages` returns 404 "unknown session". The
recovery logic in `sendMessage.ts` correctly creates a new session and retries, but if the retry
also fails the outer error handler shows a banner with only a "Dismiss" button — no way to
recover without a page reload.

**Root cause:** `sendMessage.ts` outer catch maps `apiError.kind === 'session_not_found'` to
`action: undefined` instead of `action: 'reset'`. The TopBar only renders the
"Start a fresh session" button when `action === 'reset'`.

**Fix applied on Replit:** Changed the action selector in `sendMessage.ts`:
```diff
- apiError.kind === 'turn_in_flight' ? 'reset'
+ apiError.kind === 'turn_in_flight' || apiError.kind === 'session_not_found' ? 'reset'
```
Rebuilt the SPA. Now clicking "Start a fresh session" from the banner calls `resetSession()`,
which mints a new session and clears the stale ID.

**Upstream fix:** Apply the same one-line change to `Chemclaw3_ui` repo and ship.

---

### ISSUE-U-3: Approval buttons POST to `/api/approvals/{id}/decision` which 404s on backend

*(See also ISSUE-B-5 above — fix is in the backend)*

**Symptom:** The BFF correctly whitelists and forwards the three `/approvals` routes. All three
return `404` from the Chemclaw3 FastAPI backend. The Approve / Reject buttons in the UI are
non-functional.

---

## Repo: Chemclaw3_mock

### ISSUE-M-1: `CHEMCLAW_NOTE_REPO_DIR` not documented as a deployment requirement

*(Same root cause as ISSUE-B-1 — the mock's deployment docs also don't mention this)*

**Symptom:** A fresh deployment following the mock's README will have ELN sync fail immediately
because `CHEMCLAW_NOTE_REPO_DIR` is not mentioned as a required env var anywhere in the mock or
main backend docs.

**Fix:** Add to the mock's `README.md` (and/or the main deployment guide) a "Prerequisites"
section that lists `CHEMCLAW_NOTE_REPO_DIR` alongside `CHEMCLAW_HPC_*` and `CHEMCLAW_ELN_*` as a
required variable, with a one-command setup snippet.

---

### ISSUE-M-2: Bare azide anion not flagged by hazard screen — test fixture missing

*(Same root cause as ISSUE-B-6 — fix is in the backend SMARTS table)*

**Symptom:** The mock's ELN fixtures don't include an azide test case, so the hazard screen gap
for `[N-]=[N+]=[N-]` is not caught by any automated test. The issue was found only via manual
`screen_hazards` call.

**Fix:** Add a hazard-screen test fixture (or integration test) for:
- `[N-]=[N+]=[N-]` → expected HIGH SEVERITY
- `C[N+]#[N-]` (methyl azide) → expected HIGH SEVERITY

This ensures any future SMARTS rule regression is caught in CI.
