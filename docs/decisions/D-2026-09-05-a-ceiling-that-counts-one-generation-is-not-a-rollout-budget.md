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
  the length of an upgrade taken at the HPA ceiling**, and the startup guard could not see it —
  every pod's own configuration validated. The qualifier is load-bearing and the first draft of
  this ADR dropped it in five places: the alert compares a *live* sum against a static declaration,
  and the front door is HPA-managed from `minReplicas: 2`. Recomputed per live replica count under
  the old inherited surge, the pre-commit peak was 192 / 216 / 240 connections at 2, 3 and 4
  replicas — all inside 256 — and only 288 and 312 at 5 and 6. The worst case is the right number
  to provision against; stating it as what happened on *every* upgrade claims more than it
  measured, which is this repository's own rule applied to its own correction.
- `ChemclawFleetAboveItsConnectionCeiling`'s expression was true for the whole of every rolling
  update, so it paged whenever one outlasted its 10-minute `for:`. An alert armed against a correct
  deployment is an alert its reader learns to wave through, which costs the next real excursion.

The README's own list of live-fleet excursions already named "a rollout leaving both generations up"
as something only the alert can catch. That was true while the surge was another system's decision;
it stopped being true the moment this change declared `maxSurge`.

**And the first draft of this ADR wrote that sentence about the *turn* ceiling instead — exempting
it on grounds this same commit falsified.** `CHEMCLAW_SERVICE_FLEET_MAX_CONCURRENT_TURNS` is the
sibling declaration, `maxReplicas × workers × per-process` with zero headroom, and it is read by
`ChemclawFleetAboveItsTurnCeiling` against a live sum — whose own annotation already names a rollout
as a usual cause. Both ceilings read the same declared `rollout.maxSurgePods`, rendered onto the
same `deployment-service.yaml` by the same helper. Only one of them was raised, and the ADR argued
the other away in a sentence its own diff made false. It goes to the peak too: 72 → **84**.

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
  the previous number carried. That preserves the number and not the property: each half of a
  bundle now costs its surge as well as itself, so the headroom absorbs one more bundle where it
  used to absorb three.
- `CHEMCLAW_SERVICE_FLEET_MAX_CONCURRENT_TURNS` rises 72 → **84** for the same reason, and
  `test_the_shipped_fleet_ceiling_matches_the_fleet_the_chart_renders` now asserts the peak beside
  the steady product it already asserted. `Settings` keeps validating the steady one, because a pod
  validates the shape it was handed; nothing but the test could see the other.
- **`rollout.maxSurgePods` is validated, because the obvious operator input was silently the
  rejected alternative.** Helm's `int` is `toInt64`, which yields 0 for anything it cannot parse —
  so `maxSurgePods: 25%`, the likeliest thing to write given that Kubernetes' default is 25% and
  this whole document is about it, rendered `maxSurge: 0` on every pool-holding Deployment while
  leaving the ceiling counting the steady state. A negative was worse: an invalid Deployment *and*
  a lowered ceiling, loosening the guard and the alert together. Both now fail the render by name.
  Both numeric kinds are accepted, because `values.yaml` gives `float64` and `--set` gives `int64`
  — the first version of the guard refused `--set rollout.maxSurgePods=3`.

Declared rather than inherited is the load-bearing half. The ceiling *multiplies* by the surge, and
a bound a chart's arithmetic depends on has to be a bound the chart states; inherited, the peak is
39 pools rather than 36 and nothing in the repository could see which.

**This is a provisioning change.** A release already provisioned for 256 must raise its Postgres
`max_connections` to 336, or lower `CHEMCLAW_PG_POOL_MAX_SIZE` until the product fits — the startup
check names both sides in every pod. What rose is the visibility of the peak, not the peak: the
release has always opened it.

## Consequences

- `tests/test_deploy_chart.py::test_every_pool_holding_deployment_surges_by_the_number_the_ceiling_was_computed_against`
  asserts three things against a real render: every rolling Deployment carries exactly
  `rollout.maxSurgePods`, the background worker is the *only* `Recreate`, and every Deployment whose
  pods read this release's config — which is what carries the DSN, so it is what opens a pool — is a
  role the arithmetic has a term for. **The third was missing from the first draft**, which claimed
  "either direction breaks the arithmetic" over the two directions it had tested; the untested one
  is the one that costs connections. Demonstrated: a `deployment-audit-reader.yaml` at `replicas: 2`
  carrying the shared strategy and the shared `envFrom` passed the entire chart suite while putting
  three uncounted pools outside the ceiling. All three are mutation-verified.
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
  down, which is the only kind of evidence that a pin works. It then held a *second* time, against
  the correction to this ADR: "a bundle now costs four pools rather than two" is the same claim in
  the same file, written an hour after the lesson about it.

## Alternatives considered

**Leave the surge inherited and encode Kubernetes' 25%-rounded-up rule in the helper.** No chart
behaviour changes and the ceiling still lands on 312. Rejected: it puts another system's defaulting
rule into Helm arithmetic, where nothing in this repository can check it, and it buys a ceiling 24
connections wider than the one a declared surge needs.

**Sizing the headroom back up so it absorbs three bundles again.** Rejected as a change nobody asked
for: it would raise the provisioning ask a second time in the same commit, on a guess about how many
connectors the next year adds. The shrink is stated in `values.yaml` instead, which is what lets the
next person spend it deliberately.

**`maxSurge: 0` on the pool-holding Deployments.** The peak collapses to the steady state and the
ceiling needs no rise at all. Rejected: every connector here is a single-replica Deployment, so a
zero surge means the capability is *down* during its own upgrade. That trades an availability
property for connections a site can simply provision, and the chart already prefers the opposite
trade everywhere else.

**Raise `maxConnections` and say nothing about strategy.** The cheapest edit, and the one that
leaves the next reader deriving 288 from a surge no file names. Rejected for the reason the pool
count itself was moved out of prose: an arithmetic whose inputs are not all in the repository is one
nobody can re-check.
