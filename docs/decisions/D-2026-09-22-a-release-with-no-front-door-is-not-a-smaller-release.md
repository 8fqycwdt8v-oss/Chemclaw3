# D-2026-09-22-a-release-with-no-front-door-is-not-a-smaller-release — the chart refuses a zero front door

**Status:** accepted · **Date:** 2026-09-22 · Supersedes nothing. Closes the `BACKLOG.md` row *"A front
door scaled to zero renders a release in which every pod refuses to start"*, whose own stated work was
*"deciding whether a front-doorless release is legal"*.

## Context

`service_fleet_replicas` is `Field(default=1, gt=0)` (`core/config/service.py`), and
`deploy/helm/chemclaw/templates/config.yaml` renders the chart's front-door process count into
`CHEMCLAW_SERVICE_FLEET_REPLICAS` in the shared ConfigMap. The row said `--set service.replicas=0`
therefore gives every pod in the release a value `Settings` rejects.

**The row's reproducer does not reproduce, and that is the first thing the measurement found.**
`service.autoscaling.enabled` ships **true** (`values.yaml`), and both readers of `service.replicas` are
gated on the HPA being off: `chemclaw.frontDoorProcesses` takes `autoscaling.maxReplicas` in that branch,
and `deployment-service.yaml` omits `replicas` entirely because the HPA owns it. Driven:
`--set service.replicas=0` changes **not one byte** of the shipped render, and
`CHEMCLAW_SERVICE_FLEET_REPLICAS` stays `"6"`. On shipped defaults `service.replicas: 2` renders nowhere
at all — dead configuration that carried no prose saying so.

Two overrides do reach it, and both were driven to a full render with `helm template` exit 0 and
`kubeconform` clean:

| overrides | `FLEET_REPLICAS` | `..._AT_ROLLOUT_PEAK` | `PG_FLEET_POOLS` |
|---|---|---|---|
| shipped defaults | `"6"` | `"7"` | `"26"` |
| `autoscaling.enabled=false` + `service.replicas=0` | **`"0"`** | `"1"` | `"8"` |
| `autoscaling.maxReplicas=0` | **`"0"`** | `"1"` | `"8"` |

**The blast radius is worse than the row states, because the failure is not in any component's own
code.** `Settings()` is a module-level singleton in `core/config/__init__.py`, so what fails is every
process that *imports* `chemclaw.core.config`. Driven over nine entrypoints — the API, the background
worker, the MCP face, the schedules CLI, the migrator, the connector worker and server entry, the egress
preloader, the message migration — **all nine exit 1**. And `deploy/entrypoint.sh` runs
`python -m chemclaw.cli.egress_preload` under `set -euo pipefail` *before* its `case`, so every container
that goes through the image's ENTRYPOINT dies in the shell prologue, before its component is dispatched.
That includes the `migrate`, `schedules` and `convert` hook Jobs, so `helm upgrade` never converges
either: the row names "workers and connector servers", and the hooks are the part that turns a broken
release into a stuck one.

`maxReplicas=0` is strictly the worse of the two arms. The HPA renders `minReplicas: 2, maxReplicas: 0`,
which `kubeconform` passes because that cross-field rule is the API server's rather than OpenAPI's; if
the API server rejects it the HPA never exists, and the HPA-on branch of `deployment-service.yaml` omits
`replicas`, so Kubernetes defaults the front door to one crash-looping pod. (The API-server rejection
itself is unmeasured here — no cluster.)

**Neither gate could have caught it.** `helm template` succeeds, `kubeconform` sees a valid string in a
valid ConfigMap, and `make helm-validate` renders only the shipped defaults plus the flag-union arm — it
never sets a replica count. No test in the tree sets any of these to zero, and nothing pins the `gt=0`
bound either, so the refusal was unguarded in both directions.

## Decision

**A zero front door is refused at render time, and `service_fleet_replicas` keeps `gt=0`.**

`chemclaw.frontDoorProcesses` is the one helper both arms resolve through, so the guard goes there — one
`fail` covering both, naming the key the operator actually set and the setting that refuses the value,
beside the chart's nineteen existing guards.

**Because there is no front-doorless release to allow.** Every optional component in this chart has an
`enabled` gate; the front door has none. `deployment-service.yaml` and the Service open with no `{{- if }}`
— only the PDB and the Route are individually switchable — and nothing in `deploy/` or `docs/` asks for a
workers-only or connectors-only release. So this number is structurally the front door's own pod count,
and zero is not a smaller release: it is a broken one.

`service.replicas`' deadness under the shipped HPA is documented in `values.yaml` and pinned by a test,
rather than guarded. A `--set` that silently does nothing is worse than one that is refused, and an
operator scaling a default release has to turn the HPA off or move `maxReplicas` — which is now written
where they will look.

## Consequences

**The rollout-peak over-declaration at zero becomes unreachable, which is why it is recorded rather than
fixed.** The peak arithmetic unconditionally charges three front-door pools plus a readiness pool for a
Deployment at `replicas: 0` that never surges: 137 declared connections against an honest 120. It is an
over-declaration, so it fails safe against the 256 ceiling — and with zero refused there is no input that
reaches it. The row's claim that "the arithmetic is right at zero" holds for the steady-state keys
(`readiness=0`, and the per-server figures match a real fleet) and not for the peak ones; that is the
correction, and it costs nothing now.

**The precedent this follows is `connectors.<name>.url`.** A bundle pointed at an external URL pods
nothing here, and the fleet budget deliberately omits it because *"counting it would spend the fleet's
connection budget on processes that do not exist."* That is the shape a real `service.enabled: false`
would need — a component that is absent rather than zero — and it is the thing to build if a site ever
wants one. This decision does not build it; it refuses the spelling that looks like it and is not.

**Revisit when:** a deployment asks for a release with no front door — a workers-only or connectors-only
install. The answer then is a `service.enabled` gate that omits the Deployment, the Service, the Route,
the HPA, the PDB and both monitors, and that removes the front door's pools from the fleet budget the way
an external connector's are removed. It is not `replicas: 0`, which leaves every one of those objects
rendered and the ConfigMap poisoned.

**What holds it:** `tests/test_deploy_chart.py::test_a_release_with_no_front_door_refuses_to_render`
parametrises both reachable arms, because the row's own arm is neither;
`test_the_front_door_count_the_chart_refuses_is_the_one_settings_refuses` reads `gt=0` off `model_fields`
so the chart guard cannot drift stricter than the code it protects; and
`test_the_fixed_replica_count_renders_nowhere_while_the_hpa_is_on` reds if `service.replicas` ever
becomes live under the HPA, which is the condition the `values.yaml` comment is true under.
