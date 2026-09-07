# D-2026-09-07-a-contract-check-that-cannot-reach-the-contract — `/openapi.json` is served again, from a gated `APIRoute`

## Status

Accepted. Supersedes §4 of `D-2026-08-06-the-caller-chooses-the-kid-not-the-workload` — that ADR
stands in full on JWKS refresh, `_match_kid` and the ungatable-surface pin, and its §4 *invariant*
is kept rather than reversed. What changes is the disposition of one route.

## Context

`D-2026-08-06` §4 found `/openapi.json` unauthenticated: FastAPI serves the schema from a plain
`Route`, not an `APIRoute`, so `require_principal` never applied and `tests/test_route_auth_coverage`
skipped it for the true-but-insufficient reason that it has no dependency tree. `create_app` closed
it with `openapi_url=None`. That ADR's rejected alternative reads, in full:

> **Gating `/openapi.json` behind `require_principal`.** Not possible without re-implementing the
> route as an `APIRoute`, to serve a document with no consumer.

Two reasons, and the second is the load-bearing one. It is now false.

`Chemclaw3_ui/scripts/check-openapi.mjs` fetches `GET {base}/openapi.json` and diffs that repo's BFF
route whitelist against the paths the backend publishes. Its own docstring says why it exists: *"the
same miss happened three times"* — `capability_degraded`, `tool_failed` and `job_failed` each reached
production absent from `shared/events.ts`, and because `normalizeEvent` drops anything outside its
union, each was **silently** absent.

Driven against this service, on this branch, before the change:

```
$ uvicorn chemclaw.api.app:create_app --factory --host 127.0.0.1 --port 8080
$ node --experimental-strip-types scripts/check-openapi.mjs http://127.0.0.1:8080

  ✗ GET http://127.0.0.1:8080/openapi.json — status 404
  (exit 1)
```

So the check written specifically to stop a recurring production miss **has never once run against
a backend**, and the route-surface half of the UI contract has been unguarded for its whole life.
Worse, the script's own failure text tells the reader *"The FastAPI service serves this"* — it does
not — so the honest signal it was designed to give ("this check did not run") reads as a
misconfigured invocation.

The gap is not hypothetical. On the first run after this change the check immediately reported four
whitelist entries it could not match against a served route, and seven backend routes the BFF does
not forward — a list nobody could read before because the list could not be produced.

## Decision

**Serve the OpenAPI document from a first-party `APIRoute`, behind `require_principal`.**
`openapi_url` stays `None`.

```python
@app.get("/openapi.json")
async def openapi_schema(principal: CurrentUser) -> dict[str, Any]:
    return app.openapi()
```

`CurrentUser` is the whole mechanism: it makes the endpoint an `APIRoute` with `require_principal`
in its dependency tree, which is both the gate and the reason the auth-coverage sweep can see it.
The handler is registered in `create_app` after the route modules rather than in a `routes/` module,
because the document is not a resource of any domain — it is the app describing itself.

### The invariant that made the original decision reasonable is kept, and is stronger than before

- `_ungatable_surface` still pins to exactly `{("Mount", "")}`; the mutation proof that a bare
  `Route` at `/openapi.json` fails that pin is unchanged and still passes.
- The schema route now appears in `_unauthenticated_routes`' input rather than beside it, and
  `test_the_probe_allowlist_names_exactly_the_open_routes` requires it to be absent from the open
  set. Under `openapi_url=None` the route was not gated — it was *missing*, which no sweep can
  distinguish from a route that forgot its dependency.
- Asserted over the wire in the shipped posture: `entra_required=True`, no token, **401**.

`docs_url`/`redoc_url` stay off. They are pages for a human to read, and no consumer was found for
those.

## Alternatives weighed

**Publish a generated schema artifact instead, and leave the route closed.** The honest competitor:
a `make` target dumping `app.openapi()` to a committed file, a CI check regenerating and diffing it,
and a path for the UI to read it. Rejected on three counts.

1. It is a *mirror*, and every recurring defect in this contract is a mirror that drifted — the UI's
   hand-written `shared/events.ts` has now been wrong nine times. Adding a fourth artifact to keep
   in step is the failure mode, not the fix.
2. The check would then assert against a file rather than against a running service. That is exactly
   the defect `D-2026-09-05-a-ratchet-that-re-derives-half-its-basis-bounds-half-a-request` was
   written to end: *a basis that is re-derived rather than observed will agree with itself forever.*
   A schema dumped by this repository's own test process is not evidence about what the pod serves —
   two of this month's ADRs are about a measurement that was taken off a fixture instead of off the
   wire and was wrong by 20,500 tokens.
3. Distribution has no free hop. The UI would fetch it from a raw git URL (egress the netguard
   posture refuses to normalise) or someone would copy it by hand, which is the manual step whose
   omission is the entire finding.

**Leave it closed and delete the UI's check.** Rejected: it is the only cross-repo guard on the
route surface, and the miss it guards against has happened three times.

## Consequences

- **The route/parameter/model surface is readable by any authenticated principal.** That is a real
  widening from "nobody" and it is stated rather than buried. It is bounded by the same gate and the
  same per-principal rate budget as every other route; the original concern — readable by *anyone
  who can reach the pod* — stays closed, and is now closed by an assertion over the wire rather than
  by the route's absence.
- The document lists itself (`GET /openapi.json` appears in `paths`). The UI check reports an
  unforwarded backend route as informational, not a failure, so this costs one line of that list.
- `app.openapi()` caches into `app.openapi_schema` on first call: one generation per process.
- The UI check sends no `Authorization` header, so it runs green against a dev or compose backend
  (`entra_required=false`, dev principal) and 401s against an enforced one. Making it carry a token
  is a `Chemclaw3_ui` change and is not blocked by anything here.

## What was measured rather than assumed

- The 404 and `exit 1` above, from the real script against the real service.
- After the change, the same invocation: `✓ GET /openapi.json — 28 paths`, then a route-surface diff
  that ran for the first time (4 whitelist entries unmatched, 7 backend routes unforwarded).
- `entra_required=True`, no token → 401, over the ASGI stack rather than by reading a dependency
  tree.
- The premise that three `/proposals` entries were forwarding to a 404 was **stale when checked**:
  `Chemclaw3_ui` merged "Delete the review queue's dead half" (#68) first. The gap it illustrated is
  real and the illustration is not; the four unmatched entries above are what the check reports
  today, and at least some of them are an artifact of that script's own sample-id list rather than a
  backend gap.
