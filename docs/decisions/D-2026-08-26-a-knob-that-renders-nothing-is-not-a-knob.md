# D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob — three chart switches that did less than the file beside them said

## Status

Accepted. Three `BACKLOG.md` §4 rows, closed together because they are one failure with three
faces: a value in `values.yaml` whose *documented* effect and *rendered* effect differ, in a file
whose comments are the only thing anybody reads.

## Context

The chart's values file is unusually well commented — 700-odd lines of argued prose about why each
knob exists. That is a strength and it is also how all three of these survived: a reader checking
what a switch does reads the comment, agrees, and never renders the template.

**1. `connectors.<name>.enabled` never reached the agent.** The file said, in as many words:

> Enabling a connector here is only half the switch: CHEMCLAW_CONNECTORS_ENABLED in `config` below
> decides which bundles the agent loads at all.

That key was in **none** of the 33 `config` entries. So the agent ran the setting's default, the
empty string, which `registry.enabled()` reads as *every discovered bundle* — "discovery is
enablement until you say otherwise". `enabled: false` removed a bundle's pods and left its tools on
the model's surface: for `qm`, the launcher would start the wrapper on the polled queue and its
child on `connector-qm`, which nobody polls, and the chemist is told "running" until the 25 h
ceiling. Latent only because all seven shipped entries are `enabled: true`.

**2. One `replicas` knob drove two differently-shaped Deployments.** `deployment-connectors.yaml`
read `$cfg.replicas` for both the MCP server and the Temporal worker, so scaling `calc`'s server to
4 for request load also ran four queue pollers and spent four more of `postgres.maxConnections`.
Worse, the *guard* asking for `replicas` at all applied only to a bundle without a `url:`, while the
worker block is deliberately not conditioned on `url` — so an externally hosted bundle owning
durable work would render `replicas:` empty (Kubernetes reads that as 1) and contribute
`nil | int` = 0 to `chemclaw.pooledProcesses`. The connection budget would have been short by
exactly the pods that were running.

**3. `networkPolicy.egressDestinations` was declarable, empty, and therefore allow-all.** `to: []`
in a NetworkPolicy means *any destination*, so the shipped default permitted TCP/443 from every pod
to the whole internet while the object in the cluster read as "egress is restricted". This one had
already been through one round of this exact fix: an earlier pass added the knob and a comment
saying empty "is not a safe end state". A year of installs would each have read neither.

## Decision

**A knob's rendered effect is its documented effect, and where the chart cannot make that true it
refuses to render.**

- **`CHEMCLAW_CONNECTORS_ENABLED` is derived**, by `chemclaw.connectorsEnabled`, from the same
  `.Values.connectors` block that renders the pods — exactly as `CHEMCLAW_CONNECTOR_URLS` already
  is, and for the same reason: a second list of one topology goes stale the first time a bundle is
  toggled, and this is the copy whose staleness is invisible. Pathsep-joined, in the map's key
  order, which is Helm's sorted order and therefore `registry.discovered()`'s order — so the
  advertised tool order does not move.
- **A release that enables no connector is refused**, not rendered. The empty string means "load
  everything", so "none" is the one intent this variable cannot express, and rendering it would
  invert the operator's choice by way of the fix.
- **`serverReplicas`/`workerReplicas` size one half each, defaulting to `replicas`.** The shared
  value stays, because for every bundle shipped here the two halves *are* the same size; what
  changed is that they no longer have to be. Both rendered halves are `required`, which is what
  closes the empty-`replicas` hole rather than leaving it to a test of the values file.
- **The egress posture must be stated.** Exactly one of `egressDestinations` (where these pods may
  talk) or `allowAnyDestination: true` (the deliberate, greppable statement that they may talk
  anywhere on those ports). Neither, or both, and `helm install` fails naming both ways out.

## Why a `fail` rather than a better default

The chart cannot invent a site's Postgres CIDR, so it cannot ship destinations; and shipping
`allowAnyDestination: true` as the default would be the same silence with a longer name. What it
*can* do is make the choice unavoidable at the loudest moment available — the install — and put the
operator's answer in their own values file, where the next person greps for it.

The cost is that `helm template` on the shipped defaults needs one `--set`. The Makefile's two
renders and the runbook's deploy example pass it, `deploy/README.md` documents it, and
`tests/test_deploy_chart.py` asserts that every shipped-defaults render carries the flag — so the
next render added without it fails offline rather than in CI.

Correcting the prose was not bookkeeping. Two places said the policy was a containment it never
provided — `deploy/README.md` (*"default-deny egress with an allow-list … Nothing else leaves a
pod"*) and the `networkPolicy` heading in `values.yaml` itself (*"the app talks only to these,
nothing else leaves the pod"*). That is the same claimed control one layer up, and documents
asserting a containment the object does not provide is how the first round of this fix ended up
being only a comment.

## Consequences

`enabled: false` now takes a bundle's tools away with its pods. A bundle may scale its two halves
independently, and the connection budget follows both. No release inherits an egress permission it
did not write down.

Nothing here was verified by rendering: `helm` is a live-edge dependency this suite does not have,
so all four new assertions parse the template text and the values, which is what every other check
in `tests/test_deploy_chart.py` does. What that cannot see is the *logic* of a condition, which is
why the egress guard is written to be readable as one line — `empty` on both sides, failing when the
two agree. `make helm-validate` on a machine with `helm` is what proves the render itself, and it is
in `make ci`.

## What was measured rather than assumed

- `grep -rn CHEMCLAW_CONNECTORS_ENABLED deploy/` before the change → one hit, in the comment
  claiming the key was in `config`.
- `settings.model_copy(update={"connectors_enabled": ""}).connectors_enabled_list` → `[]`, i.e.
  empty really is "every bundle", which is the premise of the all-disabled guard and is now
  asserted beside it rather than remembered.
- Both `$cfg.replicas` reads in `deployment-connectors.yaml`, and both in
  `chemclaw.pooledProcesses`.
