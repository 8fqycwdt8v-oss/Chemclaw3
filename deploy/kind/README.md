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
| https://127.0.0.1:18443/entra/mock-tenant | the mock tenant (oidc-mock mode's sign-in authority) |

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
dev opt-outs, the UI in `AUTH_MODE=dev` (`chemclaw/ui:<tag>-devauth`), Temporal plaintext.

`CHEMCLAW_KIND_AUTH=oidc-mock`: the chart's own identity posture against the mock tenant, nothing
patched — `values-kind-oidc-mock.yaml` over `values-kind.yaml`. Sign-in is enforced, the insecure
opt-outs are off, and `CHEMCLAW_ENTRA_PRIVILEGED_ROLES=chemist` (the mock's preset testers alice and
bob hold it; carol holds nothing). Core refuses that posture over plaintext Postgres or Temporal to a
non-loopback host, so the mode also runs both over TLS:

| Piece | How |
| --- | --- |
| Certificates | `up.sh` issues one CA per cluster and leaf certificates for Postgres, the Temporal frontend (plus a client certificate), and the mock; the CA key is never stored. `chemclaw-kind-ca` holds the CA certificate — trust it in a browser to sign in without a warning. |
| Postgres | TLS in every mode; the DSNs carry `sslmode=require` in this one. |
| Temporal | Frontend TLS with client certificates required (`chemclaw-temporal-tls-env`); the chart's `secrets.temporalTls.enabled` is back to its production `true`. |
| Mock tenant | `mock-eln` serves https; the browser reaches it at `https://127.0.0.1:18443/entra/mock-tenant` (NodePort 30443), which is also the issuer every token carries. Core fetches the keys in-cluster (`https://mock-eln:8090/...`) and trusts the cluster CA via `CHEMCLAW_ENTRA_CA_BUNDLE`, which reuses the CA already mounted for Temporal. |
| UI | `chemclaw/ui:<tag>`, `AUTH_MODE=msal`, `ENTRA_AUTHORITY` = the tenant URL above. |

The smoke in this mode mints a token for alice from the tenant, checks an anonymous `POST /sessions`
is a 401, and runs the same turns and job as alice. Switching modes on a running cluster restarts
the pods whose environment or certificates changed. A fresh cluster gets the tenant's host port from
`kind-config.yaml` (30443 → 18443); a cluster created before that mapping existed cannot gain one, so
`up.sh` serves the same address with a supervised `kubectl port-forward` (restarted whenever it
exits, stopped by `down` and by switching back to devauth).

## What running it found in the chart and core

Fixed in core, with tests, because each one breaks any Kubernetes install and not only this one:

- **Service links.** The chart's front-door Service is `chemclaw-service`, so Kubernetes handed every
  pod (re)started after install `CHEMCLAW_SERVICE_PORT=tcp://<ip>:8080`, which `Settings` reads as
  `service_port`: every component died at import. Every chart pod now sets
  `enableServiceLinks: false` (`tests/test_deploy_chart.py`).
- **`421 Misdirected Request` from every connector's `/mcp`.** `FastMCP(name)` enables MCP's
  DNS-rebinding guard with a loopback-only `Host` list, so a caller dialling a connector by Service
  name was refused while `/healthz` stayed green. Core's `connector_app` now admits loopback plus the
  connector's own `connector_urls` address. The fleet servers have the same default; their fix is
  `Chemclaw3-mcp` #153, which reads `MCP_ALLOWED_HOSTS` — set by the fleet's own deployment files
  and, for a checkout that predates it, by `render-fleet.sh`.
- **The `bo` server is OOM-killed by the shared 512Mi connector limit** (measured 864 MiB at start).
  Connector servers can now declare `connectors.<name>.serverResources`, and `bo` does.
- **`python -m chemclaw.cli.mock_llm --host`**, so the mock can serve other pods.

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
  cluster has no adapter for). The startup probes get twice production's budget: a fresh `up`
  starts some two dozen Python processes at once and the node sits at 800–1000 % CPU for minutes.
- **Certificates are issued once per cluster.** Should one of the TLS Secrets go missing, `up.sh`
  issues a new CA and every certificate, and restarts the pods that read them at start — Postgres,
  Temporal, the mock, the UI, and every pod of the release (the Temporal client certificate).
- **A re-run converges.** `helm upgrade` runs with `--force-conflicts`, so a field changed by hand
  while debugging does not block the next `up`.

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
