# API front door — design & simplification (round 1)

Slice: `src/chemclaw/api/app.py`, `deps.py`, `middleware.py`, `routes/*.py`.
Lens: structure that costs more than it buys. Everything below was read in full; every claim
marked *measured* was produced by a script under `/tmp` and its output is quoted verbatim.

Two things I checked and found **sound**, so they are not findings: the `register(app)`-instead-of
-`include_router` decision is correct on the installed FastAPI (0.141.1 — `app.include_router` leaves
a single `_IncludedRouter` node and `sum(isinstance(r, APIRoute) for r in app.routes) == 0`, so the
route-walking tests really would go blind), and the `app ↔ routes` import cycle really does resolve
from every entry point (`import chemclaw.api.routes.jobs`, `…routes.ops`, `…deps`, `…app` each
succeed standalone).

---

## Three route docstrings name a patch target that does not reach the route

- **Severity**: medium
- **Location**: `src/chemclaw/api/routes/jobs.py:5-7`, `src/chemclaw/api/routes/approvals.py:6-8`,
  `src/chemclaw/api/routes/streams.py:108-110`
- **Trigger**: a test (or a future maintainer) does what `jobs.py`'s module docstring says —
  "the suite patches them there (`chemclaw.agent.durable_tools.job_status` and friends)" — and
  patches `chemclaw.agent.durable_tools.job_status`, then calls `GET /jobs/{id}`.
- **Consequence**: the patch is inert. `app.py:39` binds the name at import time
  (`from chemclaw.agent.durable_tools import cancel_job, job_status, request_note_reindex`), so
  rebinding the attribute on the *defining* module leaves `chemclaw.api.app.job_status` pointing at
  the original. The route runs the **real** function — a live Temporal connect — and a test written
  this way passes or fails for reasons unrelated to what it thinks it is exercising. The same
  sentence appears in `approvals.py` (`chemclaw.agent.interaction_tools.approval_owner and friends`)
  and inside `streams.py:108-110` (`the suite's patch seam
  (chemclaw.agent.session_events.stream_new_events)`).
- **Evidence**: `routes/README.md:32-37` — the document all three docstrings point the reader at —
  states the opposite and states it correctly: *"The suite's seam is the front-door module
  (`monkeypatch.setattr("chemclaw.api.app.job_status", …)`)"*. Every real test agrees with the
  README (`tests/test_jobs_api.py:88`, `tests/test_approvals.py:57-60`,
  `tests/test_service.py:529` via `monkeypatch.setattr(app_module, "stream_new_events", …)`).
  Measured, `/tmp/probe_seam.py`:

  ```
  status: 404 body: {"detail":"no such job"}
  fake reached: []
  app_mod.job_status is dt.job_status: False
  ```

  (the script patched `chemclaw.agent.durable_tools.job_status` with a fake returning
  `status="completed"`; the fake was never called and the route fell through to the real
  implementation, which could not reach Temporal and 404'd.)
- **Fix**: replace the three sentences with the README's wording (`chemclaw.api.app.<name>`).
  Behaviour-preserving (comments only). Better still, apply the finding below and the sentences
  become unnecessary.

---

## The `front_door` re-export seam: a test-only indirection that inverts the layering

- **Severity**: medium
- **Location**: `src/chemclaw/api/app.py:97-118` (`__all__`), and the 13 call sites that read through
  it — `routes/approvals.py:33,42,59`, `routes/jobs.py:35,50,81`, `routes/notes.py:51`,
  `routes/ops.py:49`, `routes/proposals.py:206`, `routes/results.py:43`,
  `routes/sessions.py:133`, `routes/streams.py:111`, `deps.py:175-178`
- **Trigger**: not a runtime failure — a structural cost paid on every read of the module.
- **Consequence**: the composition root imports its leaves (`app.py:60-71`) and every leaf imports
  the composition root back (`from chemclaw.api import app as front_door`), for no reason other
  than giving `monkeypatch` one address. The concrete costs, all present today:
  1. 13 collaborator names (`approval_owner` … `stream_new_events`) plus 4 types/helpers are
     re-exported from `app.py` purely so tests can reach them; `app.py`'s own docstring calls itself
     "the composition root and nothing else" two paragraphs above the block that makes it not that.
  2. `deps.py:175` has to do a *function-body* import of `app` to avoid closing the cycle at module
     scope — an import statement whose only justification is the seam.
  3. Every route reads `front_door.job_status(...)` rather than `job_status(...)`, which hides the
     real dependency graph: `grep` for who calls `search_job_records` finds `app.py` (which does
     not call it) and not `routes/jobs.py` (which does).
  4. The seam is subtle enough that three of its own docstrings got it backwards (finding above).
- **Evidence**: `app.py:90-96` states the purpose outright — *"The module's surface, and … its
  **test seam**. The suite patches the routes' external collaborators on this module by name."*
  `routes/README.md:36-37` — *"The import is circular on purpose"*.
- **Fix**: have each route module import its collaborators directly
  (`from chemclaw.agent.durable_tools import job_status`) and move each test's patch target to the
  route module that uses it (`monkeypatch.setattr("chemclaw.api.routes.jobs.job_status", …)`) —
  the ordinary "patch where it is looked up" rule. That deletes 13 entries from `__all__`, the
  `from chemclaw.api import app as front_door` line in nine modules, the lazy import in `deps.py`,
  and the whole cycle. Behaviour-preserving for the served app (identical call targets); the only
  edits outside `src/` are ~15 patch-target strings in five test files.

---

## The "handed off, never advanced" window is solved twice — and the copy that matters is the weak one

- **Severity**: medium
- **Location**: `src/chemclaw/api/routes/streams.py:28-63` (`_SlotBoundEventStream`) vs
  `src/chemclaw/api/routes/turns.py:209-244` (`claimed` / `handed_off` flags)
- **Trigger**: a client issues `POST /sessions/{id}/messages` and disconnects while
  `http.response.start` is still in flight — i.e. after `post_message` returns the
  `EventSourceResponse` but before sse-starlette first advances the body generator.
- **Consequence**: the generator's `finally` (`turns.py:200-207`) never runs because an async
  generator that never started runs no `finally` at all, and the outer `finally`
  (`turns.py:241-244`) is skipped because `handed_off` is `True`. The session's `active_turns`
  entry survives, and **every further turn on that session is 409'd until the lease expires** —
  measured at **604.8 s**, because `_claim_turn_slot` sizes the lease at the turn timeout
  (`service_turn_timeout_seconds=600`) plus the admission wait. `streams.py` has the exact fix for
  the exact same window on the sibling SSE route, and its docstring even describes the failure
  ("An async generator that never started runs no `finally` at all — measured, and it survives
  `gc.collect()`"), but the class is private to that module with one caller and was never applied
  here.
- **Evidence**: measured, `/tmp/probe_turnslot.py` (memory session store, so `turn_claims is None`
  and only the in-process slot is in play):

  ```
  response built: EventSourceResponse
  active_turns after handoff-without-advance: {'sess-1': 2306.895602056}
  second turn refused: 409 a turn is already running for this session
  seconds until the session unwedges: 604.8
  ```

  `turns.py:236-240` acknowledges the gap ("the one window neither covers (handed off, never
  advanced), which the lease in `_claim_turn_slot` bounds instead") without naming that the bound is
  ten minutes of refusals for a chemist who closed a tab.
- **Fix**: hoist `_SlotBoundEventStream` out of `streams.py` (it is generic — it takes a `release`
  callable), generalise `release` to "run this teardown once when the response finishes being
  served, however it ends", and use it in `turns.py` for `active_turns.pop` +
  `_release_turn_claim`. That deletes both `claimed` and `handed_off` and the outer `try/finally`,
  leaving one owner of cleanup instead of two mutually-exclusive ones tracked by flags. The hoist is
  behaviour-preserving; using it in `turns.py` is a deliberate behaviour change — it closes the
  604.8 s window.

---

## Per-app gauges are published through a process-global registry, so `/metrics` can report another app's state

- **Severity**: medium
- **Location**: `src/chemclaw/api/app.py:290-331` (`METRICS.bind_gauge(...)` × 5, each closing over
  this `app`), against `src/chemclaw/core/metrics.py:485-490` (`self._gauges[name] = source`)
- **Trigger**: call `create_app()` twice in one process — which the test suite does dozens of times,
  and which any embedding (mounting the front door beside a second ASGI app, an in-process
  integration harness) would do.
- **Consequence**: `bind_gauge` keys only on the metric *name*, so the second `create_app()` silently
  replaces the first app's gauge closures. `GET /metrics` on app A then reports app B's live
  sessions, in-flight turns and unhealthy connectors, with no error anywhere. The binding also keeps
  a strong reference to the last app (and its whole live-session LRU) alive for the process's
  lifetime.
- **Evidence**: measured, `/tmp/probe_gauge.py` — three sessions created on app A, none on app B:

  ```
  A len(live_sessions) = 3  B = 0
  A /metrics -> chemclaw_live_sessions 0
  B /metrics -> chemclaw_live_sessions 0
  ```

  `app.py:284-286` claims *"Gauges read the live structures rather than a mirrored counter, so there
  is nothing to keep in sync"* — the *binding* is the thing that is out of sync. Supporting evidence
  that the coupling is already load-bearing across test modules:
  `tests/test_deploy_chart.py:953` asserts `"chemclaw_fleet_turn_ceiling" in METRICS.render()`,
  which is only true because some *other* test module happened to construct an app first.
- **Fix**: give the front door its own registry instance owned by the app
  (`app.state.metrics = Metrics()`, bound in `create_app`, rendered by `routes/ops.metrics` through
  `state(request)`), or — smaller, if the single-registry design is wanted — have `bind_gauge`
  refuse a rebind of a name already bound to a *different* source, so the collision is loud instead
  of silent. In a single-app production process the served numbers are unaffected either way; what
  the change buys is that the gauge becomes assertable in-process.

---

## `register()`'s nine-line rationale is duplicated verbatim in ten modules, and has already drifted

- **Severity**: low
- **Location**: the `register` docstring in all ten route modules —
  `ops.py:160-170`, `sessions.py:214-224`, `turns.py:248-258`, `streams.py:148-158`,
  `results.py:50-58`, `plan.py:130-139`, `approvals.py:66-76`, `proposals.py:211-220`,
  `notes.py:57-65`, `jobs.py:87-96`
- **Trigger**: reading, or changing, the shared rationale.
- **Consequence**: ~90 lines of identical prose (plus the same paragraph an eleventh time in
  `routes/README.md:9-12`), and the clone has already started to diverge: `results.py` and
  `notes.py` omit the closing sentence *"Registering on the app keeps both exactly as they were when
  these handlers lived in `create_app`."* that the other eight carry. A reader diffing two modules
  now has to decide whether that omission means something.
- **Evidence**: `diff <(sed -n 50,58p routes/results.py) <(sed -n 87,96p routes/jobs.py)` — identical
  except for the trailing sentence and the singular/plural of "route(s)".
- **Fix**: state the rationale once (`routes/README.md` already does, correctly) and reduce each
  `register` docstring to one line: `"""Attach this module's routes to `app` — see
  `routes/README.md` for why these are not an `APIRouter`."""`. Behaviour-preserving.

---

## One ownership gate, three spellings — and two routes take a session object they never read

- **Severity**: low
- **Location**: `src/chemclaw/api/routes/plan.py:18-22` (`get_plan`) and `plan.py:73-79`
  (`decide_plan`), both taking `live: CurrentSession`; against
  `results.py:59-61`, `streams.py:159-161`, `sessions.py:228-230`, which attach the same gate as
  `dependencies=[Depends(resolve_session)]`; against `sessions.py:102-106` and `turns.py:40-46`,
  which take `CurrentSession` **and use it**.
- **Trigger**: adding a session-scoped route, or auditing which ones are gated.
- **Consequence**: three idioms for one invariant. `results.py:39-41` writes the rule down —
  *"The ownership gate is attached at registration … rather than taken as a parameter, the same way
  the event stream does it: nothing in the answer depends on the live session object"* — and
  `plan.py` violates it: neither `get_plan` nor `decide_plan` reads `live` (both read the plan from
  the checkpointer via `session_todos(session_id)`, `plan.py:53` and `plan.py:101`), so the
  parameter is pure gate. `tests/test_service.py:1466-1504` has to enforce the invariant by
  enumerating route *paths* and hand-listing them, precisely because the dependency shape is not
  uniform enough to assert on.
- **Evidence**: `plan.py:53` uses `session_id`, not `live`; `mypy --strict` does not flag an unused
  parameter, so nothing catches the drift.
- **Fix**: drop `live: CurrentSession` from both `plan.py` handlers and register them with
  `dependencies=[Depends(resolve_session)]`, matching `results.py`/`streams.py`. Behaviour-preserving:
  `resolve_session` still runs before the handler and still 404s a non-owner (route-level
  dependencies are solved in the same pass as parameter dependencies), and `require_principal` stays
  in the dependency tree that `tests/test_route_auth_coverage.py` walks, because `resolve_session`
  itself depends on `CurrentUser`.

---

## `_resolve_session` is a single-caller private wrapper; `_is_reviewer`/`_visible_proposal` are private names imported by other modules

- **Severity**: low
- **Location**: `src/chemclaw/api/deps.py:96-110` (`_resolve_session`) and `deps.py:146-154`
  (`resolve_session`); `deps.py:81` (`_is_reviewer`), imported at `routes/jobs.py:14` and
  `routes/proposals.py:23`; `deps.py:184` (`_visible_proposal`), imported at `routes/proposals.py:23`
- **Trigger**: reading `deps.py` to find out what its public surface is.
- **Consequence**: `resolve_session` is `return await _resolve_session(request, session_id,
  principal)` and nothing else — the two functions differ only in the annotation on `principal`
  (`Principal` vs `CurrentUser`), and `_resolve_session` has exactly one caller in `src/`. Meanwhile
  three genuinely-shared helpers wear a leading underscore while being imported across package
  boundaries, so the underscore no longer distinguishes "internal" from "shared": `_is_reviewer` is
  the authorization predicate for `DELETE /jobs/{id}` (`jobs.py:75`), the proposal queue's scoping
  (`proposals.py:95`) and the decide gate (`proposals.py:129`).
- **Evidence**: `grep -rn "_resolve_session" src/` returns three hits, two of them in docstrings; the
  only call is `deps.py:154`.
- **Fix**: inline `_resolve_session` into `resolve_session` (behaviour-preserving; the `Principal`
  vs `CurrentUser` annotation is the same runtime type), and rename `_is_reviewer` → `is_reviewer`
  and `_visible_proposal` → `visible_proposal_or_404` (the public `visible_proposal` is the
  `Depends` wrapper, so it needs a distinct name). Both renames are mechanical and
  behaviour-preserving.

---

## `GET /approvals/{id}` issues two Temporal queries where one would do

- **Severity**: low
- **Location**: `src/chemclaw/api/routes/approvals.py:78-83` (registration) with
  `deps.py:162-181` (`owned_approval`) and `approvals.py:36-45` (`get_approval`)
- **Trigger**: any `GET /approvals/{approval_id}` or `POST /approvals/{approval_id}/decision`.
- **Consequence**: `owned_approval` opens a Temporal client and queries the workflow for `owner`
  (`agent/interaction_tools.py:111-118`), then the handler opens a client again and queries the
  *same workflow handle* for `status` (`interaction_tools.py:150-157`) — two round trips per read
  where one query could return both. `get_approval`'s own `except ValueError → 404` is then reachable
  only on a race (the hold closing between the two queries), so the second half of the duplication
  buys nothing but a duplicated error string: `"no such approval hold"` is written three times
  (`deps.py:180`, `deps.py:181`, `approvals.py:45`, `approvals.py:61`).
- **Evidence**: `interaction_tools.py:113-116` and `:152-155` are the same three lines with a
  different query method.
- **Fix**: add one `view` query to `InteractionApprovalWorkflow` returning `(owner, status,
  summary)`, have `owned_approval` stash it on `request.state`, and let `get_approval` read it. If
  that is more than it is worth, the cheaper version is to keep the two queries and delete
  `get_approval`'s dead-in-practice `except ValueError` branch, noting the race. Behaviour-preserving
  either way except for the number of RPCs.

---

## `readyz`'s two probes are the same ten-line cache twice

- **Severity**: low
- **Location**: `src/chemclaw/api/routes/ops.py:34-52` (`_connector_health`) and `ops.py:55-88`
  (`_database_reachable`)
- **Trigger**: reading or changing the readiness caching policy.
- **Consequence**: both functions are `front = state(request)` → `window =
  settings.service_readiness_cache_seconds` → `now = time.monotonic()` → `if window and now -
  front.<at> < window: return front.<value>` → probe → write both fields → return, with only the
  probe and the two attribute names differing. A change to the caching rule (say, jitter, or a
  negative-result backoff) has to be made in two places, and `_database_reachable`'s docstring
  already says "Cached on the same window and for the same reason as the connector sweep" — an
  invitation for the two to drift.
- **Evidence**: `ops.py:42-46` and `ops.py:71-75` are line-for-line parallel.
- **Fix**: one helper — `async def _cached(front, value_attr, at_attr, probe)` — with the two
  callers passing their probe. Two callers is exactly the Rule-of-Three boundary, so this is a
  judgement call; the reason to take it here is that the *policy* (not just the code) is the thing
  being duplicated. Behaviour-preserving.

---

## A stale line-number citation in `deps.py`

- **Severity**: low
- **Location**: `src/chemclaw/api/deps.py:6`
- **Trigger**: following the citation.
- **Consequence**: the docstring says *"(`_within_budget` lives inside `require_principal`,
  `chemclaw.api.auth:129-154`)"*. `require_principal` is at `auth.py:209` and `_within_budget` at
  `auth.py:240`; lines 129-154 of `auth.py` are `_signing_key`'s JWKS/`kid` handling, which is
  unrelated to either. It is the only hardcoded line-number citation in this whole slice, so the
  cheapest fix is to remove the convention rather than repair the number.
- **Evidence**: `grep -n "_within_budget\|require_principal" src/chemclaw/api/auth.py` →
  `209:async def require_principal`, `240:def _within_budget`; `sed -n 125,145p` shows
  `PyJWKClientConnectionError` handling.
- **Fix**: drop `:129-154` and cite `chemclaw.api.auth.require_principal` by name.
  Behaviour-preserving.
