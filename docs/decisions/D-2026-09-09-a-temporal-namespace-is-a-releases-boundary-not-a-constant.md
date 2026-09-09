# D-2026-09-09-a-temporal-namespace-is-a-releases-boundary-not-a-constant — A Temporal namespace is a release's boundary, not a constant

**Status:** accepted · **Date:** 2026-09-09

## Context

`values.yaml` shipped `CHEMCLAW_TEMPORAL_ADDRESS: "chemclaw-temporal-frontend.temporal.svc:7233"`
— a broker in its **own cluster-shared `temporal` namespace**, not one per release — beside
`CHEMCLAW_TEMPORAL_NAMESPACE: "chemclaw"`, a string constant. `CHEMCLAW_BACKGROUND_TASK_QUEUE` is
the constant `background-jobs`, `OWNED_SCHEDULE_IDS` is a set of bare constants, and
`job_workflow_id` carries no site. `deploy/jenkins/environments/README.md` documents
`dev.yaml`/`staging.yaml`/`prod.yaml` as the shape of a deployment. Those compose into one Temporal
namespace, one task queue and one schedule-id space for every release in the cluster.

Measured against the live broker with probe-prefixed ids through the shipped `apply_schedules` /
`_prune`:

```
site A applied:  probe-f1-eln-sync -> workflow=SiteAWorkflow  interval=0:30:00
site A settled:  ['probe-f1-eln-sync', 'probe-f1-eval-drift']
site B applied:  probe-f1-eln-sync -> workflow=SiteBWorkflow  interval=0:05:00   <- overwritten
after B pruned:  ['probe-f1-eln-sync']                                           <- A's eval-drift deleted
```

A peer's `helm upgrade` rewrote this release's `eln-sync` to a **different workflow type at a
different interval**, and site B having `eval_drift_enabled: false` **deleted** site A's Schedule,
because `OWNED_SCHEDULE_IDS - planned_ids` cannot tell a peer's Schedule from a leftover of its own.

Beyond Schedules: workers on one queue take each other's tasks, so a peer's `RetentionWorkflow`
runs against this release's Postgres and knowledge checkout; and under `ALLOW_DUPLICATE_FAILED_ONLY`
a job requested at B rejoins A's completed execution and returns A's result with A's `job_records`
row.

**The code already half-knew.** `durable/schedules.py` says the owned set is "a fixed explicit set
(never a prefix match against a shared Temporal namespace)" — it anticipated the shared namespace
and protects *other software's* schedules, which is no protection at all against a second ChemClaw,
because a second ChemClaw plans the same ids.

**The Postgres half, verified rather than repeated.** `infra/sql/*.sql` creates 44 tables and a
grep for `deployment_id|site_id|release_name|tenant` across them returns nothing.
`durable/retention.py::_EXPIRED_THREADS` selects thread ids with no discriminator, so release A's
sweep disposes of release B's expired threads under A's window — and with the queue shared it may
not even be A's worker running it.

## Decision

1. **`temporal.namespace` has no default and the chart refuses to render without it.** It is
   derived into the ConfigMap and refused inside `config:` as well, so the gate and the pods read
   one value and it cannot be rendered twice.
2. **The Postgres constraint is documented, not enforced** — no chart guard can see a database —
   and it is stated in `values.yaml`, `deploy/README.md`, `deploy/jenkins/environments/README.md`
   and the runbook rather than only here.
3. Every render site states it: the Makefile's three validation renders, the Jenkins render stage
   (from a parameter with no default, so an unnamed namespace meets the refusal in the pipeline),
   `openshift.sh`'s own `temporal_flags()`, and the runbook's by-hand recipe.

**`openshift.sh` gets its own helper rather than a third call to `posture_flags`,** and this is the
part worth stating: the two existing postures are booleans acknowledging a permissive default, and
their helper's exclusive-or ("stated in the file, or opted into, or refuse") is the right shape for
that. This is a *value* with no permissive default to accept, so it reads `temporal.namespace` from
the values file scoped to the `temporal:` block — not a bare `namespace:` match, which every other
block in a values file is entitled to spell — then `TEMPORAL_NAMESPACE`, then refuses.

## Alternatives declined

**State it in prose.** This is the mistake `D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob`
was written about ("the comment saying so lived in `values.yaml`, which nobody re-reads after the
first install"), and it is worse here: the inherited default is not merely permissive, it is
actively destructive the moment a second release exists.

**A boolean acknowledgement** (`soleTenantOfNamespace: true`). Three environments would each set it
true and still collide. The string the operator types **is** the discriminator, so forcing the
string is the only statement that buys anything.

## What this costs

Every render of the shipped defaults now takes a third `--set`, and every existing release must
state a namespace at its next upgrade — `chemclaw` reproduces today's behaviour exactly, which is
what the validation renders pass.

## Left open, and it is the stronger control

Stamp each Schedule with the release identity (Temporal supports a schedule memo) and have
`_apply`/`_prune` refuse to touch one stamped by another release. That turns a stated intention into
a **detected** collision. It lives in `durable/schedules.py`.
