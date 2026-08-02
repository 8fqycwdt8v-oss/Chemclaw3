"""The front door's one authorization gate, as a type alias every route shares (H1).

`CurrentUser` is `Depends(require_principal)` spelled once. Before this module every route wrote
`principal: Principal = Depends(require_principal)` verbatim — twenty copies of the same line
enforcing "no route skips authentication or the per-principal rate budget" (`_within_budget`
lives inside `require_principal`, `chemclaw.api.auth:129-154`). A convention repeated twenty times
is a convention the twenty-first route can forget; nothing failed if it did.

This does not make the gate itself any stronger — `require_principal` is unchanged, and this is
still a `Depends`, not middleware (see `tests/test_request_limits.py` for why the gate must stay a
dependency: it needs to run *inside* FastAPI's request handling to raise a clean `HTTPException`
rather than reject at the ASGI layer). What it buys is a single spelling to grep for, and a
`route.dependant` tree with exactly one shape to look for `require_principal` in — which is what
`tests/test_route_auth_coverage.py` walks to make "every route is gated" an assertion instead of a
convention.

A handler that takes `principal: CurrentUser` and never reads the value (a handful of routes:
`GET /schedules`, `GET /profiles`, `GET /jobs`, `GET /jobs/{job_id}`) is not a mistake — it is
"authenticated, deliberately unscoped": the route needs a caller to exist, but nothing about the
answer depends on *which* caller it is. The alias is what makes that legible; the old spelled-out
`Depends()` looked identical whether the ownership check was intentionally absent or simply
forgotten.
"""

from typing import Annotated

from fastapi import Depends

from chemclaw.api.auth import Principal, require_principal

# The authenticated caller for this request (401/429 handled inside `require_principal`). Every
# route that is not in the health/metrics probe allowlist takes this — see
# `tests/test_route_auth_coverage.py` for the enforced list.
CurrentUser = Annotated[Principal, Depends(require_principal)]
