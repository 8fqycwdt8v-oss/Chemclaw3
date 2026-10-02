# `deploy/kind/` — the whole system on a local Kubernetes cluster

**Responsibility:** a reproducible local deployment of all four repositories on
[kind](https://kind.sigs.k8s.io/), using the **production images and the production Helm chart**,
brought up by one command. It is the cluster-shaped counterpart of `infra/live/e2e-full-stack/`
(which runs the same four repos as host processes): what it adds is everything only a cluster
exercises — the chart's hook Jobs, its NetworkPolicies, its probes and security contexts, the fleet's
own manifests, Services instead of loopback ports.

```console
$ make kind-up        # create cluster, load images, deploy, wait, smoke — idempotent
$ make kind-status    # pods, jobs, the release, and the three host URLs
$ make kind-smoke     # the smoke alone, against a running cluster
$ make kind-down      # delete the cluster (and everything in it)
$ make kind-validate  # offline: render the chart with these values + the fleet, schema-check all of it
```

| Host URL (127.0.0.1 only) | What |
| --- | --- |
| http://127.0.0.1:15173 | the UI (BFF + SPA) |
| http://127.0.0.1:18000 | the front door (`/healthz`, `/readyz`, the API) |
| http://127.0.0.1:18091 | the Temporal UI |

The ports avoid 5432 / 8000 / 5173 / 8091, which the compose lanes hold.

## What is deployed

All in one namespace, `chemclaw` (the fleet's NetworkPolicies admit callers by a same-namespace
`podSelector`, so this is a requirement).

| Piece | From | Notes |
| --- | --- | --- |
| Core: front door, background worker, connector servers and workers, interactive workers, the migrate / convert / schedules hook Jobs | `deploy/helm/chemclaw` + `values-kind.yaml` | the production chart, overlaid, never forked |
| Postgres + pgvector | `manifests/postgres.yaml` | three principals as in production: owner (migrations), `chemclaw_app` (runtime, granted by the migrate hook), `temporal` |
| Temporal + Temporal UI | `manifests/temporal.yaml` | `temporalio/auto-setup:1.25.2`, namespace `chemclaw` registered at start |
| The MCP fleet, one Deployment + Service + NetworkPolicy per server | `Chemclaw3-mcp/servers/<name>/deploy/*.yaml` via `render-fleet.sh` | the fleet's own files, with image, replicas, bearer and requests patched by kustomize |
| Mock ELN/ORD/Entra app and the mock vendor MCP | `manifests/mock.yaml` | the ELN/ORD exports land on a claim the background worker mounts read-only |
| Scripted mock LLM (`python -m chemclaw.cli.mock_llm`) | `manifests/mock-llm.yaml` | the default model gateway |
| UI | `manifests/ui.yaml` | |
| Front-door relay | `manifests/front-door.yaml` | why it exists is below |

## Images

`up.sh` loads these with `kind load docker-image` and deploys whatever exists, saying what it left
out (a missing fleet image also disables its connector, because `CHEMCLAW_CONNECTORS_REQUIRED=true`
would otherwise keep the front door unready):

- `chemclaw/core:<CHEMCLAW_KIND_CORE_TAG>` (default `kind`) — `docker build -f deploy/Containerfile -t chemclaw/core:kind .`
- `chemclaw/mcp-<name>:<CHEMCLAW_KIND_TAG>` for every `Chemclaw3-mcp/servers/<name>` with a `deploy/`
- `chemclaw/mock:<tag>`, and `chemclaw/ui:<tag>-devauth` (built with `ALLOW_DEV_AUTH=true`) for the devauth mode

`imagePullPolicy: Never` throughout, so an image that was not loaded fails as `ErrImageNeverPull`
naming it rather than as a pull back-off against a registry.

**Disk.** An image exists twice — on the host and in the node's containerd. The core image is the
large one. Once loaded, the host tag may be removed; `up.sh` treats an image the node already holds as
available.

## Modes

**LLM.** `CHEMCLAW_KIND_LLM=mock` (default) points the release at the in-cluster mock.
`CHEMCLAW_KIND_LLM=live` sources `chemclaw-live-env.sh` from beside the checkouts (the key is read from
the macOS Keychain there) and overrides the gateway, model and window on the release; the key goes
into the `chemclaw-secrets` Secret through a pipe and is never an argument, a file or a log line. A
live run skips the scripted turns in the smoke, which need the mock's behaviour markers.

**Auth.** `CHEMCLAW_KIND_AUTH=devauth` (default): `CHEMCLAW_ENTRA_REQUIRED=false` with the two stated
dev opt-outs, and the UI in `AUTH_MODE=dev`. `oidc-mock` — sign-in enforced against the mock tenant
— is **refused by `up.sh` today**, with the reason: under `CHEMCLAW_ENTRA_REQUIRED=true` core refuses
a plaintext Postgres DSN and a plaintext Temporal channel to any non-loopback host, so this mode needs
Postgres TLS and Temporal frontend mTLS in this cluster, plus the UI's `AUTH_MODE=msal` against the
mock tenant. When those exist the mode sets: `CHEMCLAW_ENTRA_REQUIRED=true`,
`CHEMCLAW_ENTRA_AUDIENCE=api://chemclaw`, `CHEMCLAW_ENTRA_ISSUER` / `CHEMCLAW_ENTRA_JWKS_URL` under
`http://mock-eln:8090/entra/mock-tenant/`, `CHEMCLAW_ENTRA_PRIVILEGED_ROLES=process-chemist` (the
role the mock's test users hold), and `MOCK_ENTRA_ENABLED=true` with a matching issuer on the mock.

## Things worth knowing

- **The front door is reached through a relay, not a NodePort on its Service.** The chart's
  `chemclaw-service-ingress` policy admits only the release's pods and the namespaces
  `networkPolicy.ingressNamespaces` names; NodePort traffic arrives SNAT'd from the node and matches
  neither. Measured on kindnet (which enforces policies): a NodePort straight onto the pods timed out
  from the host while a pod in the namespace was admitted. `front-door` is a TCP relay inside the
  namespace, so the production policy stands unwidened.
- **`enableServiceLinks: false` on every pod in `manifests/`.** A Service called `temporal-ui` makes
  Kubernetes inject `TEMPORAL_UI_PORT=tcp://…`, which the Temporal UI reads as its listen port and
  crash-loops on.
- **Temporal's databases are created by Postgres's init script**, not by auto-setup: auto-setup's
  create step connects to a database named after its role, which does not exist.
- **The ELN exports reuse the chart's `documentShare` mount** (read-only, background worker only). No
  share source is enabled, so nothing crawls it as documents; `CHEMCLAW_ELN_EXPORT_DIR` reads it.
- **Requests are sized to a workstation** (the whole system on 8 CPU / 12.5 GB); limits are
  production's. Replicas are one per role and the HPA is off (it scales on a Prometheus metric this
  cluster has no adapter for).

## CI

`make kind-validate` runs in the `chart` job: it renders the chart with `values-kind.yaml`, renders the
fleet from a `Chemclaw3-mcp` checkout through `render-fleet.sh`, and schema-checks those and
`manifests/` with `kubeconform`. `tests/test_kind_deploy.py` holds the cross-file invariants (ports,
secret keys, Service names, no OpenShift-only kinds) in the `check` job.

**A full kind bring-up is deliberately not a CI job.** It needs the core image (≈12 GB unpacked) and
eleven fleet images built per run, which alone exceeds a hosted runner's free disk and the `check`
job's time budget; a cached-image variant would test images other than the PR's. What a cluster run
proves beyond the offline gate is runtime behaviour, and that is what `make kind-up` is for on a
workstation. Revisit when the images are published to a registry a runner can pull by digest.
