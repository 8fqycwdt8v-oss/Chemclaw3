# D-2026-09-26-a-worker-states-its-unauthenticated-posture — a Temporal worker refuses to boot with sign-in off unless the posture is stated

**Status:** accepted · **Date:** 2026-09-26 · **Decided by the owner, 2026-09-26.** Closes the
`BACKLOG.md` row *"What "network-exposed" means for a process that only makes outbound calls"*
(issue #461), which `D-2026-09-12-a-gateway-guard-in-the-front-door-is-not-a-deployment-guard` left
open.

## Context

The front door refuses to boot with `entra_required` off on a non-loopback bind
(`api/middleware._refuse_unauthenticated_exposure`). Its signal is a *bind*, and a Temporal worker
binds no request surface, so no worker ran that check. A worker's activities nonetheless resolve the
shared dev principal with every authorization gate open when sign-in is off, exactly as a request
would. So a deployment that forgot `CHEMCLAW_ENTRA_REQUIRED` had a front door that refused to start
and workers that started silently in the open posture.

The row named three candidate signals: reuse the bind (meaningless in a process that binds nothing
but its health surface — `worker_metrics_host` defaults to every interface, so it would refuse every
worker in every deployment), infer exposure from something else, or state the posture.

## Decision

**State it.** `worker_allow_unauthenticated` (`CHEMCLAW_WORKER_ALLOW_UNAUTHENTICATED`, default
`false`) is the posture a deployment says out loud, in the shape
`CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY` already has. `durable/serve.refuse_unauthenticated_worker`
raises at boot when `entra_required` is false and the flag is not set, naming both settings; with
the flag it boots and logs that every gate is open; with sign-in on it does nothing. It is called
from both worker entrypoints — `durable/background_worker.main` and
`connectors/worker.run_bundle_worker`, which every `connector-worker-*` goes through — before
`connect()`, so a refused worker never polls.

The lanes that legitimately run without sign-in state it: `infra/live/processes.sh` (which
`infra/live/e2e-full-stack/up.sh` runs), the hand-started worker commands in `README.md`, and the
suite's autouse fixture. `make chat` and `docker-compose` start no worker and are unchanged. The
chart ships `CHEMCLAW_ENTRA_REQUIRED: "true"`, so the new key is surfaced in `values.yaml` at
`"false"` beside it with a comment, for a release that turns sign-in off.

## Consequences

- Any deployment running workers with sign-in off must now set the flag or its workers exit 1 at
  boot. That is the intended cost: it is the configuration the front door already refuses.
- `tests/test_worker_posture.py` drives both entrypoint kinds as processes — refused without the
  flag, past the guard to the broker with it, past the guard with sign-in on — and derives the set
  of entrypoints from every `Worker(` in `src/`, so a third worker cannot join unguarded.

Revisit when: a worker gains a request surface of its own beyond health and metrics, at which point
the bind becomes a real signal for it and the front door's guard may apply directly.
