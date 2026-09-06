# D-2026-09-06-a-ceiling-that-counts-one-generation-is-not-a-rollout-budget — the connection budget charges both generations, and the shipped ceiling already covers it

## Status

Accepted. No provisioning change.

## Context

`postgres.maxConnections` is this chart's statement of what a site must provision, and
`Settings.fleet_connections_per_server` is the arithmetic that decides whether the fleet fits inside
it. That arithmetic counts the fleet's **steady state**: `chemclaw.fleetPools` pools, of which one
per front-door replica is the narrow `/readyz` pool asking for a single connection and the rest are
`pg_pool_max_size` wide.

A rolling update does not run the steady state. It runs both generations, and connections are the
one resource the new generation takes from the old — so for the length of every upgrade the fleet
holds each surging Deployment's pools twice, and the readiness term surges with it because that pool
is one per front-door *pod*.

Nothing charged the ceiling for that, and nothing could see it. The startup guard validates the
shape a pod was handed, and every pod's own configuration is valid throughout. The first sign would
be `ChemclawFleetAboveItsConnectionCeiling` — which reads a *live* `sum(chemclaw_pg_pool_max_size)`
— firing on a correct deployment, and its own annotation already lists "a rollout that left both
generations up" as a usual cause.

`deployment-workers.yaml` had reasoned about exactly this overlap when it chose `Recreate` for the
background worker, and wrote down the conclusion that let the gap survive: *"nothing on their queues
is host-local, so an overlap is only capacity."* True of turns. False of connections.

## Decision

**The budget charges the peak, and the surge it charges is declared rather than inherited.**

- `rollout.maxSurgePods` (1) is a chart value, rendered onto every pool-holding Deployment by
  `chemclaw.rolloutStrategy`. Declared because the arithmetic *multiplies* by it: a bound the chart
  depends on has to be a bound the chart states. Inherited, Kubernetes' 25%-rounded-up default gives
  two extra front-door pods at `maxReplicas: 6` rather than one, and the real peak sits above the
  counted one with nothing able to say so.
- `chemclaw.fleetPoolsAtRolloutPeak` and `chemclaw.frontDoorProcessesAtRolloutPeak` render into
  `CHEMCLAW_PG_FLEET_POOLS_AT_ROLLOUT_PEAK` and
  `CHEMCLAW_SERVICE_FLEET_REPLICAS_AT_ROLLOUT_PEAK`. **Both, always together.** The sum charges one
  narrow pool per front-door pod, so peak pools against a steady replica count would under-declare
  that term — which is the direction that exhausts a server rather than starving one.
- `fleet_connections_per_server(at_rollout_peak=True)` is the *same* arithmetic over the peak pair,
  not a second formula. The validator reads it, and `_fleet_pool_widths` travels with it so the
  decomposition the refusal prints describes the fleet the refusal refused.
- Undeclared (0, the code default) falls back to the steady pair. A CLI, a test and any hand-rolled
  deployment keep exactly today's answer.

**The shipped chart already fits, and that is the finding.** Measured end to end through the real
`Settings` against a real render: 26 pools and 6 front-door processes steady, 36 and 7 at the peak,
giving **166 connections steady and 239 at the peak against the declared 256** — 17 to spare.

## Consequences

- **No provisioning ask.** An earlier version of this work, written against the `pools ×
  pg_pool_max_size` product that `fleet_connections_per_server` replaced, measured the same surge as
  288 connections and raised the ceiling 256 → 336. That product charged every narrow `/readyz` pool
  the full eight; charging it the one connection it asks for makes the same surge cost **73
  connections rather than 128**, and it lands inside the ceiling already declared. The guard is what
  this adds; the rise was an artefact of arithmetic that no longer exists.
- That is also why this is a separate decision from the one that found the gap. Composed into the
  branch that found it, the 336 would have shipped — a 31% provisioning rise nobody needed, on a
  number two people would have had to re-derive to disbelieve.
- `tests/test_deploy_chart.py` holds four directions, each mutation-verified: the peak must exceed
  the steady state (or the keys are not reaching the pods and the test passes for a fleet with no
  exposure), the peak must fit the ceiling, every rolling pool-holder must carry exactly
  `rollout.maxSurgePods` with the background worker the only `Recreate`, and every Deployment
  reading this release's config must be a role the arithmetic has a term for.
- The surge is validated at render: a percentage (`25%` — the likeliest thing to write, since
  Kubernetes' own default is one) would otherwise pass Helm's `toInt64` as **0**, silently giving
  every single-replica connector Deployment a zero surge and counting no peak at all. A negative
  renders a Deployment the API server refuses *and* lowers the declared peak. Both fail the render
  by name.

## Alternatives considered

**Raise the ceiling and skip the arithmetic.** The cheapest edit and the one this work started as.
Rejected on measurement: against the current formula there is nothing to raise, and a rise nobody
needs is a provisioning ask that outlives the reason for it.

**Encode Kubernetes' 25%-rounded-up rule in the helper rather than declaring a surge.** No chart
behaviour changes. Rejected: it puts another system's defaulting rule into Helm arithmetic where
nothing here can check it, and it buys a peak wider than the one a declared surge needs.

**`maxSurge: 0` on the pool-holding Deployments.** The peak collapses to the steady state and none
of this is needed. Rejected: every connector here is a single-replica Deployment, so a zero surge
means the capability is *down* during its own upgrade — an availability property traded away for
connections that are already provisioned.
