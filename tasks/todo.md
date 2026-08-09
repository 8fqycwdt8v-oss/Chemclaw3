# Attaching a connector we do not run

Prompted by: *a new model is available behind a FastAPI MCP server — what do I have to do to make
it available in ChemClaw?* The manifest layer already answered it; the Helm chart did not.

## Plan

- [x] `deployment-connectors.yaml` — guard the app Deployment and the Service on `not $cfg.url`,
      leave the worker block alone (durable jobs are ours whoever hosts the tools).
- [x] `_helpers.tpl` — `chemclaw.connectorUrls` emits `$cfg.url` when set; `chemclaw.pooledProcesses`
      stops counting a server process for a bundle that pods none here.
- [x] `values.yaml` — document `url:`, including the two things the chart cannot do for the operator
      (egress destination, credential).
- [x] Tests — the two Python mirrors (`_rendered_derived_values`, `_pooled_processes`) honour `url`;
      four new tests pin the guards, including the *absence* of the unguarded `if $cfg.server`.
- [x] The `auth: none` claim that nothing enforced — real validator on `HttpEndpoint`, loopback
      predicate shared with the front door via `core/http.py`.
- [x] ADR `D-2026-08-09-a-connector-we-do-not-run` + ledger row.
- [x] Runbook — the external-server procedure, and a false line about jobs declaring a task queue.

## Review

**Shape of the change.** One knob, and it is the address itself. An `external: true` beside a `url:`
would be two declarations of one fact that can disagree, and this chart has already watched a
hand-maintained second copy of the topology go stale — which is why `connectorUrls` and
`pooledProcesses` are computed rather than written. Presence of the address is the flag.

`server:` was left meaning "the manifest declares an endpoint", so the both-ways mirror test against
the manifests is untouched. It answers a different question from `url`, and conflating them was the
first design I discarded: making `server: false` mean "external" would have broken that mirror and
made a jobs-only bundle and an externally-hosted one indistinguishable in values.

**The second half was not scope creep, it was the same defect.** `NoAuth`'s docstring described a
validator that did not exist anywhere in the tree. That was free while every bundle shipped a
loopback default and cost exactly one thing the moment a manifest could name somebody else's host —
an unauthenticated call carrying the turn's actor and full role set off-premises. It checks the
*declared* URL and deliberately not the effective one: a rule that failed on the chart's own
in-cluster Service addresses would flag every shipped bundle the day the override was set, and a
gate that fires on the normal case is a gate people switch off.

**The green tick was not the proof.** CI's `chart` job passed in seven seconds, which is too fast
for an install plus a render, so I read its log instead of trusting it. It had genuinely rendered —
32 resources, 31 valid, 0 errors — but with *default* values, and no shipped bundle sets `url`. So
the branch this whole change adds was rendered nowhere: the offline tests can only read template
text, and text cannot show a `{{- if }}` nesting mistake. `make helm-validate` now renders the
external case too, asserting no pods, the given URL in the address map, and no collateral damage to
a sibling bundle. Helm is unavailable in this sandbox (the proxy denies `get.helm.sh`; helm
publishes no GitHub release asset), so the recipe's shell logic was proven against a stubbed
`helm` — the happy path and all three failure modes — and CI runs it against the real one.
