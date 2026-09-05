# D-2026-09-05-a-ceiling-that-counts-one-generation-is-not-a-rollout-budget — the connection ceiling covers the rollout peak, and the surge it multiplies by is declared

## Status

Accepted.

## Context

`postgres.maxConnections` is the chart's statement of how many Postgres connections this release
needs a site to provision. Two things read it: `Settings` refuses a pod whose
`CHEMCLAW_PG_FLEET_POOLS × CHEMCLAW_PG_POOL_MAX_SIZE` exceeds it at startup, and
`ChemclawFleetAboveItsConnectionCeiling` compares the *live* `sum(chemclaw_pg_pool_max_size)`
against it at runtime.

`chemclaw.fleetPools` counted the steady state: front-door replicas × 3, the background worker, the
MCP face, and each connector half at its own replica count. Measured against the shipped chart that
is 26 pools — 208 connections against a declared 256.

A rolling update does not run the steady state. It runs both generations, and no Deployment in this
chart declared a strategy, so Kubernetes applied its default `maxSurge` of 25% rounded up: two extra
front-door pods at three pools each, and one extra pod for each of the seven connector Deployments.
Rendered and counted, that is **39 pools — 312 connections against a declared 256**.

`deployment-workers.yaml` had already reasoned about exactly this overlap when it chose `Recreate`
for the background worker, and wrote the conclusion down: *"The connector workers below keep the
default on purpose; nothing on their queues is host-local, so an overlap is only capacity."* That is
true of turns and false of connections, which are the one resource the new generation takes from the
old. The sentence is why the gap survived being looked at.

Two consequences, neither of them a wrong comment:

- A site that provisioned Postgres to exactly the declared number was **56 connections short for
  the length of every upgrade**, and the startup guard could not see it — every pod's own
  configuration validated.
- `ChemclawFleetAboveItsConnectionCeiling`'s expression was true for the whole of every rolling
  update, so it paged whenever one outlasted its 10-minute `for:`. An alert armed against a correct
  deployment is an alert its reader learns to wave through, which costs the next real excursion.

The README's own list of live-fleet excursions already named "a rollout leaving both generations up"
as something only the alert can catch. That is true of the *turn* ceiling, where the surge is another
system's decision. It was never true of this one: `maxSurge` is a number the chart may state.

## Decision

**The declared ceiling covers the rollout peak, and the surge it is computed against is declared
rather than inherited.**

- `rollout.maxSurgePods` (1) is a new top-level value, and `chemclaw.rolloutStrategy` renders it
  onto every pool-holding Deployment: the front door, both connector halves, and the MCP face. The
  background worker keeps `Recreate` for the reason it already had — two of it race on a host-local
  knowledge checkout — and is therefore the one term the arithmetic leaves unsurged.
- `chemclaw.fleetPools` counts the peak: `(frontDoor + surge) × 3`, the background worker unsurged,
  and `replicas + surge` for every other rolling pool-holder. The shipped chart renders **36 pools,
  288 connections**.
- `postgres.maxConnections` rises from 256 to **336** — the render plus the same absolute headroom
  the previous number carried, so that enabling one more connector is not also a database change.

Declared rather than inherited is the load-bearing half. The ceiling *multiplies* by the surge, and
a bound a chart's arithmetic depends on has to be a bound the chart states; inherited, the peak is
39 pools rather than 36 and nothing in the repository could see which.

**This is a provisioning change.** A release already provisioned for 256 must raise its Postgres
`max_connections` to 336, or lower `CHEMCLAW_PG_POOL_MAX_SIZE` until the product fits — the startup
check names both sides in every pod. What rose is the visibility of the peak, not the peak: the
release has always opened it.

## Consequences

- `tests/test_deploy_chart.py::test_every_pool_holding_deployment_surges_by_the_number_the_ceiling_was_computed_against`
  asserts both halves against a real render: every rolling Deployment carries exactly
  `rollout.maxSurgePods`, and the background worker is the *only* `Recreate`. Either direction
  breaks the arithmetic — another `Recreate` makes the ceiling over-count, and a background worker
  that starts rolling makes it under-count. Mutation-verified by dropping the strategy from
  `deployment-service.yaml`.
- The template pin is one assertion, `mul (add $frontDoor $surge) 3`, rather than two looser ones:
  `mul $frontDoor 3` beside a surge added elsewhere would pass a pair of them while charging the
  front door's overlap once instead of three times.
- `test_turning_on_a_pooled_component_moves_the_declared_connection_budget` now expects
  `replicas + surge`, because turning a component on adds a Deployment, and a Deployment's cost
  includes its overlap.
- The alert's description says a rolling update is no longer one of its causes, so a page from it is
  a real excursion again.
- **The measurement is not written into `values.yaml`.** The first draft of this change put "26
  pools steady, 36 at the peak" there and
  `test_the_shipped_connection_ceiling_matches_the_fleet_the_chart_renders`'s prose pin rejected it
  — the same pin that exists because that file once said "17 pooled processes" over a render of 14.
  `D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose` held against the session writing it
  down, which is the only kind of evidence that a pin works.

## Alternatives considered

**Leave the surge inherited and encode Kubernetes' 25%-rounded-up rule in the helper.** No chart
behaviour changes and the ceiling still lands on 312. Rejected: it puts another system's defaulting
rule into Helm arithmetic, where nothing in this repository can check it, and it buys a ceiling 24
connections wider than the one a declared surge needs.

**`maxSurge: 0` on the pool-holding Deployments.** The peak collapses to the steady state and the
ceiling needs no rise at all. Rejected: every connector here is a single-replica Deployment, so a
zero surge means the capability is *down* during its own upgrade. That trades an availability
property for connections a site can simply provision, and the chart already prefers the opposite
trade everywhere else.

**Raise `maxConnections` and say nothing about strategy.** The cheapest edit, and the one that
leaves the next reader deriving 288 from a surge no file names. Rejected for the reason the pool
count itself was moved out of prose: an arithmetic whose inputs are not all in the repository is one
nobody can re-check.
