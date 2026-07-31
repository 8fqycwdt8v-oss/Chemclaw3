# D-158 — The deployment envelope: a sidecar that emptied the tree, and three assertions the chart never made

**Status:** accepted · **Date:** 2026-07-31

## Context

Four defects in `deploy/`, none visible to `mypy`, `pytest` or the offline chart tests, and each
silent in a different way.

**The knowledge sidecar deleted the corpus it was publishing.** `knowledge-sync.sh` published the
read replica with `rsync -a --delete … 2>/dev/null || { rm -rf "${publish_dir:?}"/* ; cp -a … ; }`.
The image installed `git` and not `rsync`. With stderr discarded, `command not found` was
indistinguishable from a transfer error, so the fallback was not a fallback — it was the only
branch that ever ran. Every `intervalSeconds` tick (300 by default) emptied and refilled the
directory the serving container reads live. `kg/graph.py` caches on a stat fingerprint with a
short TTL, so a retrieval landing in that window saw a partial or empty graph and the agent
answered with no evidence. Nothing failed: a missing note is not an error, it is just less
evidence, which is the failure mode the knowledge layer is least able to notice.

**No pod spec asserted a security context.** No `runAsNonRoot`, no
`allowPrivilegeEscalation: false`, no `capabilities.drop: [ALL]`, no `seccompProfile`. The image
has run as a non-root UID since F6-T1 — which is a different statement from the pod *declaring*
that it must, and Pod Security Admission reads the declaration. A namespace labelled
`pod-security.kubernetes.io/enforce=restricted`, the default posture for a regulated OpenShift
cluster, rejects every workload in this chart. The image being correct is what made this easy to
miss: nothing fails until admission, on deployment day, in someone else's cluster.

**Egress was port-scoped, not destination-scoped, and there was no ingress rule at all.** The
egress rule read `to: []` with a port list. In a NetworkPolicy `to: []` means *any destination*,
so TCP/443 from every pod to the whole internet was permitted — while `tests/test_no_egress.py`
enforces "this system takes no external sources" (D-089) by scanning *source code* for
third-party host literals. That scan is a good guard against a developer adding a data source and
no guard whatsoever at runtime. Meanwhile `policyTypes` listed only `Egress`, so no ingress rule
existed; `api/app.py` leaves `/metrics` unauthenticated on the stated grounds that "the
NetworkPolicy is what keeps it inside the cluster", and that rule had never been written.

**Thirty-seven metrics, zero alert rules.** REV-2 closed "nothing scrapes `/metrics`" by adding a
ServiceMonitor, whose own comment argues that an endpoint which answers while nothing collects it
is how observability fails quietly. The same argument applies one level up and nobody made it:
there was no `PrometheusRule` anywhere in the repository, no SLO, no Alertmanager route. The
sharpest case is `agent/audit.py`, which logs a stable `audit_sink_failure` marker at ERROR and
increments a counter *specifically* so a lost GxP audit record can be alerted on. Nothing alerted.

## Decision

**rsync is a hard runtime dependency, and its absence is loud.** Installed in the Containerfile
beside `git`; checked for by name before publishing; stderr no longer discarded; the destructive
fallback deleted outright. Failing rather than falling back is safe in both callers by
construction — `once` fails the init container so a pod never serves against a half-published
tree, and `loop` already logs a warning and keeps serving the previous good snapshot. Neither path
can now destroy what is already published. rsync is also what keeps the good case cheap: it writes
only the delta, where a wholesale copy rewrites the entire corpus on every tick.

**The restricted profile is asserted on all six pod specs and all nine containers** — including
the knowledge-sync sidecar and the two init containers, because PSA evaluates every container in
the pod and a compliant app container beside a bare sidecar still fails admission. Written as two
`_helpers.tpl` defines so the six specs cannot drift.

The profile itself is **not configurable**. A chart that lets a deployment switch off
`allowPrivilegeEscalation: false` is offering a footgun, not a knob. `runAsNonRoot` is asserted
without `runAsUser`, because OpenShift assigns an arbitrary high UID from the namespace range and
pinning one fights the SCC rather than satisfying it. The single exception is
`readOnlyRootFilesystem`, which is *not* part of the restricted profile and cannot be defaulted on
while the calculation workers shell out to xtb/crest for scratch — it is a value, defaulted off,
documented with what must be provisioned first.

**Egress destinations become declarable; the front door gets an ingress rule.**
`networkPolicy.egressDestinations` takes NetworkPolicyPeer entries and is empty by default,
because the addresses are deployment-specific and this chart must not invent someone's Postgres
CIDR. Empty still renders `to: []` — the honest change is that it is now a named knob with a
documented consequence rather than an unremarked literal. DNS is split into its own rule so
narrowing the destinations cannot silently take name resolution with it; that failure presents as
every dependency being unreachable at once, which is the hardest possible symptom to trace back to
a values change.

The service ingress rule allows Chemclaw's own pods, the router, and the monitoring namespace. It
carries its own `enabled` toggle rather than riding on `networkPolicy.enabled`: the namespace
labels differ by distribution, and an operator whose router labels do not match must not have to
disable network policy wholesale — that would drop the egress rule too, which is the one they
least want to lose.

**A `PrometheusRule` ships with the alerts the metrics were designed for**, grouped by what is
being lost: records (audit trail incomplete, knowledge notes lost), correctness (turn lease
lapsing, rollback watermark unavailable), availability (turn failure ratio, shedding, connectors
unhealthy, database unreachable) and cost (fleet-wide token burn). Every rule alerts on a rate
rather than a total, so a counter that moved once during a deploy does not page anyone forever.

## Consequences

`helm install` into a `restricted` namespace now succeeds, which it could not before.

The egress hole is **narrowed, not closed**: with `egressDestinations` unset the default is still
"any destination on these ports". Closing it requires the operator's real CIDRs, so the residual is
tracked in `BACKLOG.md` rather than papered over with a default that would either break every
install or be a fiction.

Two tests pin the new surface in both directions: every alert must name a metric the app actually
declares (a PromQL expression naming a deleted series is silently always-empty, which reads exactly
like "the condition never occurred"), and every metric designed to alert must have an alert.

Neither `helm` nor `kubeconform` is reachable from the offline sandbox this was built in, so the
offline structural tests are what ran locally and `helm template | kubeconform` in CI is what
confirmed the render: **29 resources, Valid: 28, Invalid: 0, Skipped: 1.**

That number corrected something this ADR first claimed. `PrometheusRule` was initially added to the
chart's `_UNVALIDATED_KINDS` beside `Route` and `ServiceMonitor`, reasoning that the two
Prometheus-operator CRDs must share whatever catalog coverage that operator has. The reasoning was
sound and the premise was wrong: exactly *one* kind in the whole chart lacks a schema, so both CRDs
were being validated against the datreeio catalog all along — `ServiceMonitor`'s exemption had
simply never been checked against what kubeconform actually did. The set is now split in two, and a
pinned skip-count sits beside it, because a claim about someone else's tool should be stated in a
form that can be compared against its output rather than believed.
