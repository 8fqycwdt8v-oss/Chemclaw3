# `chemclaw.api.routes` — the front door's routes, one module per resource

`create_app` (`api/app.py`) was one long closure holding every route; this package is those
routes split by resource (R3.2). **No count is written here** — the live number is whatever
`tests/test_route_auth_coverage.py` walks on the built app, and the figure this line used to state
was stale by eleven routes, which is exactly what a reviewer enumerating the attack surface from a
table would have missed. The split is behavior-preserving, and the seam that makes it safe
is **`app.state`, not lexical capture**: every route reads the process's live structures through
`chemclaw.api.state.state(request)`, so nothing observable moved when the handlers left the
factory. `create_app` remains the only factory — each module exposes plain handlers plus one
`register(app)` that attaches them with the app's own decorators, and no module builds an app.
(Not `APIRouter` + `include_router`: since FastAPI 0.139 inclusion is lazy — `app.routes` would
hold opaque `_IncludedRouter` nodes, hiding every route from the tests that walk the table by
type — and a standalone router's routes carry no `dependency_overrides_provider`, which silently
disables `app.dependency_overrides`.)

| module | routes |
|---|---|
| `ops.py` | `GET /healthz`, `GET /readyz`, `GET /metrics` (the three deliberately unauthenticated probes), plus `GET /schedules` — operator surfaces, not chemist ones |
| `sessions.py` | `POST/GET /sessions`, `GET /sessions/{id}/messages`, `DELETE /sessions/{id}`, `POST /sessions/{id}/fork`, `POST /sessions/{id}/attachments`, `GET /profiles` — creating, listing, reading, branching and deleting conversations |
| `turns.py` | `POST /sessions/{id}/messages` — the SSE turn stream, the one route with real concurrency machinery (admission, leases, budget) — and `POST /sessions/{id}/turn/stop`, which is how a chemist ends one |
| `streams.py` | `GET /sessions/{id}/events` — the job push-back stream and its per-user/per-pod caps — and `GET /digests`, the same machinery over the cross-session digest feed |
| `results.py` | `GET /sessions/{id}/tool-results/{ref}` — the full text of what one tool returned, which the 200-character `ToolResultEvent.preview` cannot carry. Session-scoped so it reuses `resolve_session` rather than inventing an auth story for a bare `/tool-results/{ref}` |
| `plan.py` | `GET/POST /sessions/{id}/plan[...]` — the pre-execution harness-plan gate (D-137/D-167) — plus `GET /plans/pending`, the cross-session inbox of plans nobody has decided, which is the only one of the three not addressed by a session id because it is what finds the session |
| `pending.py` | `GET /pending`, `POST /pending/{id}/answer` — the questions an agent asked a chemist and is waiting on, addressed by request rather than by session for the same reason `/plans/pending` is |
| `protocols.py` | `GET /protocols[...]`, `GET /protocols/{id}/diff`, `POST /protocols/{id}/revisions`, `POST /protocols/{id}/status` — the design revision surface. **The two write routes are the only ones in this package that answer 403 rather than 404**, because a protocol is not owner-scoped the way a session is; worth knowing before reading their gate as an inconsistency |
| `proposals.py` | `GET/POST /proposals[...]`, `POST /events/knowledge-merged` — the PR-gate's review queue and the webhook that closes it |
| `notes.py` | `GET /notes/{id}` — one knowledge note as the `NoteView` `expand_note` returns, so a citation chip resolves to the note it cites. `CurrentUser`-gated, not owner-scoped: the graph has no owner |
| `jobs.py` | `GET/DELETE /jobs[...]` — the durable-run surface over `job_records` |

`caching.py` holds no route. It is the conditional-GET policy the two *read* routes above share —
`results.py` and `notes.py` — and it exists because the caching header a surface asked for
(`public, max-age=31536000, immutable`) is wrong on both of them, in two different ways its module
docstring sets out. One module rather than two header literals, so the two routes cannot drift into
disagreeing policies.

Two conventions to keep, both enforced by tests rather than asked for:

- **Every route outside the three probes takes `CurrentUser`** (`chemclaw.api.deps`), directly or
  through a resource gate like `CurrentSession` — `tests/test_route_auth_coverage.py` walks the
  built app's dependency trees and pins the open set to exactly `/healthz`, `/readyz`, `/metrics`.
- **Collaborators the test suite patches on `chemclaw.api.app` are read through that module at
  call time** (`from chemclaw.api import app as front_door` … `front_door.job_status(...)`), never
  imported into a route module by name. The suite's seam is the front-door module
  (`monkeypatch.setattr("chemclaw.api.app.job_status", …)`), and a name imported here would be a
  private binding a patch could no longer reach. The import is circular on purpose and safe: both
  sides bind module objects and dereference attributes only at call time.
