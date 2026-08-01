# D-2026-08-01-a-per-process-cap-multiplied-by-a-number-nobody-wrote-down — A per-process cap, multiplied by a number nobody wrote down

**Status:** accepted · **Date:** 2026-08-01 · **Extends:** SCALE-1 (admission stays per-process),
D-136 (the guards the comments already demanded), D-152 (the metric registry)

## Context

`service_max_concurrent_turns` bounds how many turns may hit the shared internal LLM endpoint at
once. It is a per-process semaphore, and SCALE-1 decided deliberately that it stays one: a
fleet-wide admission counter would cost a durable write and a heartbeat on every turn to bound a
*resource*, which is a worse trade than tuning the per-process number.

That decision was right and it left a hole. The number a per-process cap produces at fleet scale is
`replicas × uvicorn workers × cap`, and **nothing anywhere computed it**. The shipped chart runs an
HPA to `maxReplicas: 6` against a cap of `8`, so the endpoint can be offered 48 concurrent turns
while the only number written down is 8. There was no deployment-wide ceiling, nothing that failed
when the product moved, and no metric expressing one — so an operator raising the cap from 8 to 16
to "use the box better" sextupled fleet demand on a shared endpoint without touching anything whose
name contains the word fleet.

The same caveat is already written, twice, in the comments beside the rate limiter and the
admission cap: *"per process, so `maxReplicas` multiplies the real ceiling"*. Twice-written prose
enforced by nothing is the pattern D-136 exists to close.

## Decision

**Declare the ceiling, derive the fleet size, check the product in two places that see different
things.**

`service_fleet_replicas` is how many front-door pods the deployment may reach. A process cannot
discover this, so the chart supplies it — **derived** in `templates/config.yaml` from
`service.autoscaling.maxReplicas` (or `service.replicas` when the HPA is off), never hand-written
in `config:`. Derived, it is the same number the HPA obeys and cannot drift from it; hand-written it
would be a second copy of `maxReplicas` that goes stale the first time someone scales the front
door — the silent multiplication reintroduced by the mechanism meant to catch it.

`service_fleet_max_concurrent_turns` is the ceiling the endpoint's throughput budget permits,
declared by the operator who knows it. `0` = undeclared and checks nothing, the same split
`budget_enabled` and the rate limiter already take: a CLI, a test and a single-pod dev run have no
fleet to bound.

**At startup**, `Settings` refuses a configuration whose product exceeds the declared ceiling,
naming the product, every factor, and both settings that could move. The chart ships
`CHEMCLAW_SERVICE_FLEET_MAX_CONCURRENT_TURNS: "48"` — exactly `6 × 1 × 8`, so it ships as a
*statement of the current shape* rather than as slack, and raising `maxReplicas` without a matching
decision fails a test in this repository before it can CrashLoop a cluster.

**At runtime**, `chemclaw_fleet_turn_ceiling` is exported beside the existing per-process
`chemclaw_turn_capacity`, and `ChemclawFleetAboveItsTurnCeiling` alerts when
`sum(chemclaw_turn_capacity) > max(chemclaw_fleet_turn_ceiling)`. Startup validation sees the shape
the chart rendered, once. A `kubectl scale`, an HPA edited in the cluster, or a rollout that leaves
both generations up all push the live fleet past its ceiling **with every pod's own configuration
still perfectly valid**. Only summing the live capacity can see that, which is why the ceiling is a
gauge and not merely a validated number.

## Why not the alternatives

**Make admission itself fleet-wide.** Already rejected as SCALE-1, and nothing here changes the
argument: a durable claim per turn to bound an LLM endpoint's throughput buys exactness at the cost
of a write and a heartbeat on the hot path. What this ADR adds is not enforcement but *arithmetic* —
the product was never the problem, its invisibility was.

**Derive `maxReplicas` from the ceiling** (`floor(ceiling / (workers × cap))`) instead of checking
it. Tempting, and wrong twice over: `maxReplicas` is also an availability decision, so silently
lowering it trades one surprise for another, and it makes a value in `values.yaml` unsettable, which
fights the idiom every other knob follows. A hard error naming both numbers leaves the choice with
the person who has the context.

**Put the check in Helm** (`{{ fail }}` when the product exceeds the ceiling). The chart is where
`maxReplicas` lives, so this reads natural — but `helm template` is a live edge this repository
cannot run offline, so the check would be untestable here and provable only in the `chart` CI job,
which renders the passing case. In `Settings` it runs in the pod, where the number is actually used,
against plain `Settings(...)` construction in a test. The chart's job is to *supply* the fleet size,
not to reason about it.

**Clamp `service_max_concurrent_turns` down to fit.** A guard that quietly changes what an operator
set is how a deployment ends up with a cap nobody chose and a comment explaining a number that is
no longer in effect.

## Consequences

- The number the shared LLM endpoint actually sees is written down, in one place, and moving any of
  its three factors past the declared ceiling fails at startup in every pod with a message naming
  the product and every factor.
- The fleet size cannot drift from the HPA: it is derived from `maxReplicas`, and a test rejects a
  hand-written copy in `config:`.
- A cluster-side scale that no config change can see is caught by an alert instead of by a saturated
  endpoint. The alert is self-disabling — the ceiling gauge is `0` when undeclared.
- The shipped ceiling is deliberately exact (48 = 6 × 1 × 8), so it is a claim about today rather
  than headroom nobody sized. Raising the fleet is a two-line change and both lines are visible in
  review.
- What this does **not** do is stop the 49th turn. Admission stays per-process (SCALE-1); this
  bounds the *configuration*, not the request.
