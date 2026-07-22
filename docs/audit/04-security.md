# Phase 4 — Security Hardening Audit

**Target:** `/home/user/Chemclaw3` · **Scope:** injection, secrets, authn/authz, input validation, deserialization, error leaks, sensitive logging, web security, file/path, broad exception handling.

**Headline:** **No Critical or High findings.** The codebase is unusually security-conscious — parameterized SQL everywhere, slug/containment path guards, RS256-pinned JWT validation, prompt-injection framing, startup config validators. Findings are Medium and below: mostly deployment footguns plus a client-facing error-detail leak that contradicts its own docstring.

## Severity summary

| # | Finding | Severity |
|---|---------|----------|
| 1 | Turn errors leak raw exception text to the browser (SSE) | Medium |
| 2 | Entire auth model hinges on one default-`False` boolean (`entra_required`) | Medium |
| 3 | Durable audit-sink failures are swallowed (GxP trail gap) | Low–Medium |
| 4 | Unbounded request/tool input sizes (no length caps) | Low |
| 5 | No HTTP security headers (HSTS/CSP/X-Frame-Options) | Low |
| 6 | Upstream `response.text` echoed into exceptions/logs | Low |
| 7 | Auth-failure reason returned to client | Low |
| 8 | Audit trail logs user free-text args + user oid (PII) | Informational (by design) |
| 9 | Dev-default DB credentials in config/.env.example | Informational |

---

## 1. Error leak — raw exception text streamed to the client — MEDIUM  [CWE-209]

`service/runner.py:81-83`:
```python
except Exception as exc:
    yield ErrorEvent(message=f"The turn could not be completed: {exc}")
```
The `ErrorEvent` docstring (`service/events.py`) and the runner's own module docstring promise "a user-safe message rather than propagating a stack trace… must not leak internals" — but the implementation interpolates the raw exception string straight into the SSE payload sent to the browser. Any exception raised inside a tool propagates up through the audit middleware (`agents/audit.py` re-raises) and reaches this handler.

**Exploit scenario:** An authenticated user sends a message that triggers a DB-touching tool while Postgres is down. `chemclaw/db.py` raises `ConnectionError("Postgres unreachable at <host>: <cause>")`; that host + libpq cause is delivered to the browser. Other reachable leaks: internal `ValueError` messages containing workflow ids / SMILES (`agents/qm_tools.py`), and psycopg driver errors. Information disclosure of internal topology to any authenticated caller. **Fix:** emit a generic message + correlation id; log the detail server-side.

## 2. Auth model gated entirely by one default-`False` switch — MEDIUM  [CWE-1188 insecure default]

The whole identity/authorization stack is a no-op unless `entra_required=True`, and the default is `False` (`chemclaw/config.py:308`):
- `service/auth.py` — returns a fixed `Principal(oid="dev-user")` for **every** request, no token required.
- `agents/authz.py` — `authorize_trigger` returns immediately (expensive-action role gate open).
- `agents/authz.py` — `require_actor` falls back to `service_actor_id` instead of rejecting.
- `agents/skill_access.py` — no roles ⇒ only ungated skills, but with no gates configured that is every skill.

There is a good startup validator that hardens config *when `entra_required=True`* (`config.py:557-579`), but **nothing warns when it is `False`**. A deployment that ships without explicitly setting `CHEMCLAW_ENTRA_REQUIRED=true` runs fully unauthenticated with all authorization gates open and a single shared identity.

**Exploit scenario:** Container promoted to a prod-like network with default config. `POST /sessions` + `POST /sessions/{id}/messages` require no token; every user is `dev-user`, so session-ownership checks collapse to a single shared owner and any caller can drive any session and launch expensive QM jobs. Intended for local dev, but the safety of the whole deployment rests on one env var defaulting the insecure way. **Recommend:** a loud startup warning (or fail-closed) when `entra_required` is false *and* `service_host` is bound to a non-loopback interface (it defaults to `0.0.0.0`). Note: `SECURITY.md` already documents "run shared/exposed deployments only with `entra_required=true`" — this finding is about making the code enforce/announce that, not just document it.

## 3. Durable audit-sink failures are swallowed — LOW–MEDIUM

`agents/audit.py:155-160` — a failing Postgres audit sink never breaks a tool call (reasonable availability choice), but in a GxP setting the durable "who ran what" record can be silently lost while operations continue. Logged at WARNING (observable); append-only trail explicitly defers tamper-evidence to a later phase. Worth noting because the compliance posture assumes the durable trail is authoritative. **Consider** a metric/alert on this path rather than log-only.

## 4. Unbounded input sizes at the trust boundary — LOW  [CWE-770]

- `service/app.py` — `MessageIn.message: str` has no `max_length`; a POST body is bounded only by the ASGI server.
- `agents/graph_tools.py` — `expand_note(hops: int)` is an unbounded int fed to `neighborhood()`; docstring says "1–2 typical" but nothing enforces it.
- MCP tools accept `top_k`/`threshold` from the model; default from config but aren't clamped.

All behind auth (once #2 is set correctly) and operate on internal corpora, so impact is resource-exhaustion at most. **Add** a `Field(max_length=…)` on `message` and bound `hops`.

## 5. No HTTP security headers — LOW

`service/app.py` adds only CORS middleware. No HSTS, CSP, `X-Content-Type-Options`, or `X-Frame-Options`, and the app serves an HTML chat UI (`StaticFiles(html=True)`). CSP would be meaningful defense-in-depth for the browser surface (paired with the prompt-injection framing already in place). Mitigated in the target architecture by the OpenShift Route/ingress front-ending it, but the app sets none itself.

## 6. Upstream response bodies echoed into exceptions/logs — LOW

`workflows/hpc/nextflow.py`, `agents/identity/workload.py`, `agents/identity/obo.py` format `f"... {response.status_code} {response.text}"` from an upstream launcher/token endpoint into exceptions. These run server-side in Temporal activities (not directly returned to the browser), and token *failure* responses do not carry access tokens, so leakage risk is low — but an upstream error body can end up in logs/exception chains. Watch if these ever surface via #1.

## 7. Auth-failure reason returned to client — LOW

`service/auth.py` returns `HTTPException(401, detail=str(exc))` where `exc` is `AuthError(f"invalid token: {exc}")`. Discloses the JWT validation failure reason (audience/issuer/expiry mismatch) to the caller. Common and arguably helpful for debugging; does not leak the token itself. **Consider** a generic 401 in production.

## 8. Audit trail logs user free-text and user oid — INFORMATIONAL (by design)

`agents/audit.py` logs truncated tool arguments (user free text — may contain PII) at INFO, and `agents/identity/hpc_bridge.py` logs the requesting Entra `oid`. Both are **documented, intentional** GxP/compliance requirements and truncation is config-bounded (`agent_audit_max_arg_chars`). Flagged only so log-retention/PII handling accounts for it.

## 9. Dev-default DB credentials — INFORMATIONAL

`chemclaw/config.py:101` and `.env.example` ship `postgresql://chemclaw:chemclaw@localhost:5432/chemclaw`. `.env.example` shows a placeholder `ANTHROPIC_API_KEY=sk-ant-...` (not a real key). All real secret fields (`llm_api_key`, `temporal_api_key`, `hpc_api_token`, TLS material) default to empty strings. No hardcoded live secrets found anywhere in the tree. The dev DSN is a documented local default only.

---

## Verified-safe controls (real signal that these are *not* vulnerabilities)

- **SQL injection — none.** Every query uses psycopg placeholders: `calc/postgres_store.py`, `agents/session_store.py`, `agents/audit_store.py`, `agents/session_events.py`, `eln/cursor.py`, `calc/migrate.py`. The only interpolated identifiers are in `mcp_servers/fpstore.py`, guarded by `table.isidentifier()` + trusted domain constants + an `int` width — not reachable from user input. Migration files are sent whole via the simple-query protocol with no placeholders, which is correct.
- **Command injection — none.** `kg/git_submitter.py` uses `asyncio.create_subprocess_exec` (argv list, **no `shell=True`**), with a timeout+kill. Branch/path derive from `Note.id`/`type` constrained to `^[A-Za-z0-9][A-Za-z0-9_.-]*$` plus explicit git-ref rules (`kg/note.py`), and `_contained_note_path` rejects any path escaping the checkout. No `os.system`/`shell=True` anywhere.
- **Path traversal — strongly guarded.** Slug validation + containment check + a startup validator forcing `knowledge_dir` relative (`config.py:511-525`). ELN reads glob a configured directory.
- **Deserialization — safe.** `python-frontmatter` uses `yaml.SafeLoader` by default; `kg/note.py` catches `yaml.YAMLError`. No `pickle`, no `yaml.load`, no `eval`/`exec`/`__import__` on untrusted data (`cloudpickle` appears only as a transitive Temporal dependency).
- **JWT validation — solid.** `service/auth.py` pins `algorithms=["RS256"]` (blocks alg=none/HS256 confusion), enforces audience (the confused-deputy guard), issuer, and `require=["exp"]`.
- **CORS — safe default.** `service/app.py` adds the middleware only when origins are explicitly configured (default empty = no cross-origin access); no wildcard, no `allow_credentials`.
- **Prompt-injection boundary** is centralized in `agents/framing.py` for retrieved/untrusted note bodies, and the MCP write tool `index_molecule` is excluded from the agent's `allowed_tools`, so ingestion writes stay on the PR-gated path.

**Top two to fix:** #1 (stop interpolating `{exc}` into the client-facing `ErrorEvent`) and #2 (make the unauthenticated `entra_required=False` mode fail-closed or loudly warn when bound to a non-loopback interface).
