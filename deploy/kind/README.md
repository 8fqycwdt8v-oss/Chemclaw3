# `deploy/kind/` — the whole system on a local Kubernetes cluster

**Responsibility:** a reproducible local deployment of all four repositories on
[kind](https://kind.sigs.k8s.io/), using the **production images and the production Helm chart**,
brought up by one command. It is the cluster-shaped counterpart of `infra/live/e2e-full-stack/`
(which runs the same four repos as host processes): what it adds is everything only a cluster
exercises — the chart's hook Jobs, its NetworkPolicies, its probes and security contexts, the fleet's
own manifests, Services instead of loopback ports.

```console
$ make kind-up        # create cluster, load images, deploy, wait, smoke — idempotent
$ make kind-status    # pods, jobs, the release, and the host URLs
$ make kind-smoke     # the smoke alone, against a running cluster
$ make kind-down      # delete the cluster (and everything in it)
$ make kind-validate  # offline: render the chart with these values + the fleet, schema-check all of it
```

| Host URL (127.0.0.1 only) | What |
| --- | --- |
| http://127.0.0.1:15173 | the UI (BFF + SPA) — open it at exactly this address (it is the BFF's `APP_ORIGIN`) |
| http://sandbox.localhost:15174 | the UI's HTML sandbox listener: the frame an `html` artefact renders in, on its own origin |
| http://127.0.0.1:18000 | the front door (`/healthz`, `/readyz`, the API) |
| http://127.0.0.1:18091 | the Temporal UI |
| https://127.0.0.1:18443/entra/mock-tenant | the mock tenant (oidc-mock mode's sign-in authority) |

The ports avoid 5432 / 8000 / 5173 / 8091, which the compose lanes hold.

**The sandbox has a different hostname and a different port.** `sandbox.localhost` resolves to
loopback in every current browser with no hosts-file entry, so the frame is a different *site* from
the app at `127.0.0.1`, which is what the UI README asks of a deployment; kind maps host ports, not
hostnames, so the port differs as well. Open the app as `http://127.0.0.1:15173` — the sandbox shell
accepts content only from that exact origin, and the app opened as `localhost:15173` shows an empty
artefact frame. A cluster created before this mapping existed lacks port 15174 (a mapping is fixed
at creation); `up.sh` warns, and `make kind-down && make kind-up` recreates it. For a browser
policy beyond the lane, the html decision record recommends `WebRtcIPHandling=disable_non_proxied_udp`
(`docs/decisions/D-2026-10-03-model-written-html-runs-in-an-opaque-origin-the-backend-never-serves.md`).

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
| Scripted mock LLM (`python -m chemclaw.cli.mock_llm --catalogue e2e`) | `manifests/mock-llm.yaml` | the default model gateway; its markers are below |
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

## The scripted model's markers

The mock serves `--catalogue e2e`: the storm's behaviours (`cli/storm_behaviours.py` — `a-cheap`,
`a-retrieval`, `d-collide`, `f-slow`, the fault injections …) followed by the browser suite's
scripted workflows (`cli/e2e_behaviours.py`). A turn picks one with `[[name]]` in the user message.
**The newest marked user message decides**: a later marker replaces an earlier one, and an unmarked
follow-up continues the last one (so the UI's "Go ahead with the approved plan." continues a plan,
and a message queued in a shared session inherits the slow turn it queued behind). Until this rule
the *first* marker in the resent thread won, so a conversation could never change behaviour.

| Marker | What the mock does | What a browser test can then assert |
| --- | --- | --- |
| `[[e2e:plan]]` | turn 1: `write_todos`, one step declaring `compute_reaction_energy`, then "Nothing runs until you approve it"; any later turn of the conversation: calls `compute_reaction_energy` (fixed N2 + 3 H2 → 2 NH3, `quick`), then says it ran — or that the plan gate refused it | the plan card, Approve → Continue → a result preview; Decline → the gate's refusal on the trace. Under the `computation` profile (`plan_only`) |
| `[[e2e:remember]]` | `remember_preference(key="forbidden_solvent_dcm", value="Never use dichloromethane (DCM) …")` | "Arguments to remember_preference" contains DCM |
| `[[e2e:conditions]]` | no tool; answers amide-coupling conditions that begin `Standing preferences received: <entries>.` when the system message carried the section (or `No standing preferences reached me.`), and uses the first of DCM, DMF, acetonitrile, 2-MeTHF that no entry names | the plumbing (the opening sentence) and the respect (no DCM recommendation) — in a *new* conversation after `[[e2e:remember]]` |
| `[[e2e:cite]]` | `gather_evidence` with an amide-coupling reaction anchor (or `expand_note` on a `reaction-…` id written in the message, or the first `a>>b` SMILES in it) plus `find_notes("amide coupling")`; then cites the first `reaction-<source>.<id>` and the first knowledge-note id the tools returned, or says none came back | a `reaction-…` chip and a `failure-…`/`playbook-…` chip in the answer text, each opening its note |
| `[[e2e:long-job]]` | `start_optimization_campaign` on the `measured` objective, which suspends on a person after its seed batch; the seed is derived from the message, so a distinct message is a distinct job; then quotes the `bo-start_optimization_campaign-…` id the launcher returned | the job is `running` in the registry and can be cancelled |
| `[[e2e:slow]]` | no tool; streams its answer over 20 s | Stop is visible for 20 s; another participant can queue, watch and withdraw |
| `[[e2e:artefact]]` | `create_exhibit` with a `document` ("Amide coupling plan"), its arguments streamed in 40 pieces over about a second; then one line pointing at the artefact | the Artefacts pane fills in from `exhibit_draft` frames and then shows the stored artefact the `exhibit` event names |

What the UI suite should send on the mock lane (`say(mock, real)` in `Chemclaw3_ui`
`e2e/kind/lane.ts`), with a run tag wherever a scenario needs its own job or conversation:

- plan gate (`03`, `06` "bob's plan"): `[[e2e:plan]] <tag> compute the ammonia reaction energy`, then
  the card's own Continue, or `Go ahead and run it now.` after a decline.
- preferences (`07`): `[[e2e:remember]] <tag> never use DCM`, then in a new conversation
  `[[e2e:conditions]] <tag> EDC/HOBt amide coupling`; assert the answer starts
  `Standing preferences received:`.
- citations (`02`, `09`): `[[e2e:cite]] <tag> EDC amide couplings` for a record and a note;
  `[[e2e:cite]] reaction-eln-ord.suzuki-flow-hte-04620` for the citation-only record.
- cancel (`04`): `[[e2e:long-job]] <tag> start a measured campaign` — the run tag is what makes
  the job new (a payload already launched rejoins its run, D-011).
- shared session (`06`): open with `[[e2e:slow]] <tag> shared start`, or send it as alice's
  running turn.
- artefacts: `[[e2e:artefact]] <tag> draft the amide coupling plan`; assert the pane opens on
  `Amide coupling plan` and its text matches what the drafts showed.

What a green run on these markers proves is the plumbing — the card, the gate, the store, the
section, the chip, the registry, the queue — never that a model would decide to do any of it; that
stays `CHEMCLAW_KIND_LLM=live`'s question.

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
- **One node is one failure domain for the control plane too.** Rolling the whole release at once
  (a `helm upgrade` that changes every pod template) runs this 8-CPU node to a load average near 90:
  etcd times out, the API server answers transient RBAC errors, and Helm can fail at the end of a
  rollout that Kubernetes itself completes. `up.sh` retries the install once; the startup budgets
  below keep the pods from restart-looping meanwhile. Measured in the K5 resilience run.
- **Requests are sized to a workstation** (the whole system on 8 CPU / 12.5 GB); limits are
  production's. Replicas are one per role and the HPA is off (it scales on a Prometheus metric this
  cluster has no adapter for). The startup probes get twice production's budget: a fresh `up`
  starts some two dozen Python processes at once and the node sits at 800–1000 % CPU for minutes.
- **Certificates are issued once per cluster.** Should one of the TLS Secrets go missing, `up.sh`
  issues a new CA and every certificate, and restarts the pods that read them at start — Postgres,
  Temporal, the mock, the UI, and every pod of the release (the Temporal client certificate).
- **A re-run converges.** `helm upgrade` runs with `--force-conflicts`, so a field changed by hand
  while debugging does not block the next `up`.

## Operator notes

### Recovering from an upgrade storm

A `helm upgrade` that changes every pod template (a new core image, a changed ConfigMap) starts two
generations of about twenty Python processes on one node at once. Measured twice on an 8-CPU
Docker Desktop node: load average 90–120, the API server unreachable for 30–40 minutes
(`TLS handshake timeout`, `etcdserver: request timed out`, transient `RBAC: clusterrole … not found`),
Helm failing while Kubernetes was still converging, and the release left `pending-upgrade`. Waiting
it out works in principle; the steps below were faster both times.

1. **Take the workers off the node.** They are the slowest importers and the least urgent:

   ```console
   $ for d in $(kubectl --context kind-chemclaw -n chemclaw get deploy -o name | grep worker); do
       kubectl --context kind-chemclaw -n chemclaw scale "$d" --replicas=0; done
   ```

   Repeat any command that times out; the API server answers again within minutes once the load drops.
2. **Let everything else settle**: front door, connector servers, fleet, UI, all Ready. Force-delete
   pods left `Terminating`, and delete a pod that stays Running but unready while its own
   `/healthz` answers. That is a kubelet that stopped probing during the storm, and a fresh pod clears it.
3. **Bring the workers back two at a time**, waiting for each pair's `rollout status`. Measured: 41–151 s
   per pair.
4. **Clear a stuck release record** if Helm left one (`helm history` shows `pending-upgrade` or
   `pending-rollback` as the newest revision):
   `kubectl -n chemclaw delete secret sh.helm.release.v1.chemclaw.v<N>`.
5. **Re-run `up.sh up`** with the same auth mode. It finds the pods ready, completes the release with
   its hooks, and runs the smoke.

### Reclaiming node disk

`kind load` imports each image under an `import-<date>@sha256:…` reference. An image replaced by a
newer load keeps its layers until that reference and the content it holds are removed. Inside the node:

```console
$ docker exec chemclaw-control-plane ctr -n k8s.io content prune references
```

This removes the content blobs that no image reference holds; measured, it freed 6 GB. Afterwards
`ctr images check` lists the remaining images as `incomplete`, because their compressed layers are
gone. Their unpacked snapshots remain, so pods still start (verified by restarting one). What is
lost is only the ability to re-export an image from the node.

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
