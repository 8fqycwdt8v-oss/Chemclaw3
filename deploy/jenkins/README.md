# Jenkins delivery

GitHub Actions is the **gate**; Jenkins is the **delivery**. `.github/workflows/ci.yml` decides
whether a commit is allowed to exist (`make lint type cov`, every validator the `ci` recipe runs,
a chart render);
`image.yml` proves the image builds and every component imports as a non-root UID. Neither can push
to a registry or reach a cluster, and that gap is what these pipelines close — it is the
`docs/planning/DEFERRED.md` row "Push-to-registry + `helm upgrade` rollout".

Nothing here re-runs the gate by default. A second implementation of `make ci` in Groovy would be a
second answer to the same question, and the repository has a name for that failure.

## The pieces

| File | What it is |
| --- | --- |
| `../../Jenkinsfile` | This repository's pipeline: build the one multi-role image, verify it, publish it by digest, render the chart with that digest, apply, smoke. |
| `Jenkinsfile.release` | The four-repository rollout: joins the repositories' digests into one descriptor and applies it in dependency order. |
| `lib/image.sh` | `build_and_push` — one Containerfile, four possible builders, and the **digest** as the return value. |
| `lib/registry-login.sh` | The credential half of the same, including kaniko's (which has no login verb). |
| `targets/openshift.sh` | Apply a descriptor to a namespace: `helm upgrade --install` for the chart, `oc set image` for the repositories that have no chart. |
| `targets/databricks.sh` | Apply the Databricks half: asset bundles and apps, plus a preflight on the serving endpoint and SQL warehouse a release *consumes* but does not create. |
| `environments/` | One values file per environment — the site's own facts. Not in this repository; see that folder's README. |

## The release descriptor

Four repositories publish independently, so "what is in staging" has to be one reviewable object
rather than four build numbers somebody correlates by timestamp. Every pipeline writes one and
archives it:

```json
{
  "environment": "staging",
  "order": ["mcp-props", "core", "ui"],
  "components": {
    "mcp-props": {"kind": "deployment", "deployment": "chemclaw-mcp-props", "container": "server",
                  "image": "registry/chemclaw-mcp-props", "digest": "sha256:…"},
    "core":      {"kind": "helm", "release": "chemclaw", "chart": "deploy/helm/chemclaw",
                  "image": "registry/chemclaw", "digest": "sha256:…",
                  "values": "deploy/jenkins/environments/staging.yaml"},
    "ui":        {"kind": "deployment", "deployment": "chemclaw3-ui", "container": "ui",
                  "image": "registry/chemclaw3-ui", "digest": "sha256:…"}
  },
  "databricks": {"servingEndpoint": "chemclaw-llm", "sqlWarehouseId": "0123456789abcdef"}
}
```

**`order` is the apply order, and the backend precedes the UI.** The fleet first (core dials it),
then `core` — whose chart runs the migrations as a pre-upgrade hook, so the schema lands with the
backend — then `ui`. A release whose UI reads a shape only the new backend serves (the artefacts
wave: a geometry's `structure_id`, `tool_failed.call_id`, the sandbox handshake) rolls the UI
last for that reason; `Jenkinsfile.release` builds `order` that way and nothing reorders it.

**Digests, never tags.** `values.yaml` treats `image.digest` as the release knob and ignores
`image.tag` when it is set, because a tag is a pointer: `helm rollback` to a release naming `0.1.0`
fetches whatever `0.1.0` means now, and every audit record stamps a build revision that stops being
answerable the moment a tag is re-pushed (D-2026-08-01-a-tag-is-a-pointer-not-a-build, runbook
§(xiv)). Both targets refuse a value that is not a `sha256:` digest, and so does the release job.

**`kind` is `helm` for exactly one repository, and that is a real limitation rather than a design.**
Only `Chemclaw3` ships a chart. `Chemclaw3_ui` and each `Chemclaw3-mcp` server have an image and a
NetworkPolicy and no chart, so the honest minimum is `oc set image` against a Deployment an operator
created — it changes the bytes and claims nothing else. Charts for those two are a `BACKLOG.md` row.

## Parameters, and what each pipeline needs from the environment values file

The chart refuses to render until a release states its egress posture, its retention posture and its
Temporal namespace (`deploy/README.md` § "Install, step by step" lists every render-time guard).
`targets/openshift.sh` reads the environment values file first and adds a `--set` only for what that
file does not state, refusing — with a sentence naming the missing value — when neither says it.

| Parameter | `Jenkinsfile` | `Jenkinsfile.release` | Effect |
| --- | --- | --- | --- |
| `DRY_RUN` | default `true` | default `true` | render and `helm upgrade --dry-run`; nothing in the cluster changes |
| `DEPLOY_TARGET` | `none`/`openshift`/`databricks` | `openshift`/`databricks` | which target script applies the descriptor |
| `ENVIRONMENT` | `dev`/`staging`/`prod` | same | selects `environments/<env>.yaml` as the chart's values file |
| `ALLOW_ANY_EGRESS_DESTINATION` | yes | yes | adds `--set networkPolicy.allowAnyDestination=true` when the values file lists no `egressDestinations` |
| `ACCEPT_UNBOUNDED_GROWTH` | yes | **no** | adds `--set retention.unboundedGrowthAccepted=true` when the values file states no `retention.windows` |
| `TEMPORAL_NAMESPACE` | yes, no default | **no** | adds `--set temporal.namespace=<value>` when the values file names none |
| `RUN_GATE` | default `false` | — | runs `make db-migrate` and `make ci` on the agent first, for a Jenkins-only estate; needs a real Postgres in `CHEMCLAW_POSTGRES_DSN`, or the Postgres-backed tests skip and still print green |
| `IMAGE_REGISTRY`, `IMAGE_NAME`, `BASE_IMAGE`, `IMAGE_BUILDER` | yes | `IMAGE_REGISTRY` | where to push; `BASE_IMAGE` pins the base by digest (runbook §(xiv)); empty registry = build and verify only |
| `CORE_DIGEST`, `UI_DIGEST`, `MCP_DIGESTS` | — | yes | the digests to promote (`server=sha256:…` per line for the fleet) |
| `NAMESPACE`, `CLUSTER_API` | yes | yes | the OpenShift target |
| `DATABRICKS_HOST` | yes | yes | the Databricks target |

**So a `Jenkinsfile.release` run needs an environment values file that states `retention.windows`
(or `retention.unboundedGrowthAccepted: true`) and `temporal.namespace`**: that pipeline has no
parameter for either, and the target refuses without them. That file is the better answer for every
pipeline anyway — a parameter states a posture outside the reviewed release descriptor.

**What the agent needs installed:** `git`, `helm`, `kubeconform`, `oc`, `jq`, `curl`, one image
builder (`buildah`, `podman`, `kaniko` or `docker` — OpenShift agents get no Docker socket), and
`uv`/`make` for `RUN_GATE`; `databricks` for that target.

## The two targets

They read the **same** descriptor and each skips what the other owns, because a real environment is
usually split rather than exclusive.

- **`openshift`** — the services: front door, workers, connector pods, the tool fleet, the UI.
  `helm upgrade --install` also runs the chart's pre-deploy migrate Job, so the DDL completes before
  any app container starts. Never run migrations by hand.
- **`databricks`** — the workspace: asset bundles (jobs, and the serving endpoint where the
  environment owns it) and apps. It also **preflights what a release consumes but does not create**:
  the Mosaic AI serving endpoint behind `CHEMCLAW_LLM_BASE_URL`, and the SQL warehouse the
  `eln-databricks` binding names. That check exists because its absence fails silently in the worst
  way — the front door starts, passes both probes, and dies at the first turn, since `/readyz`
  probes connectors and knows nothing about a model endpoint.

Databricks does not host Postgres, Temporal or the worker fleet, and no script here pretends it
does. It carries three of this system's dependencies (the ELN warehouse, the LLM endpoint, heavy
compute) plus whatever workspace assets a release owns.

## What is proven and what is not

Written and **unrun**: there is no cluster, no registry and no workspace in this repository's
environment, and a pipeline is not evidence about someone else's infrastructure. What *is* checked
offline is the part that can be: `tests/test_jenkins_delivery.py` asserts every `make` target these
files invoke exists, that the deploy path passes a digest and stated postures, that the fleet
Deployment and container names a descriptor patches exist in `Chemclaw3-mcp`, and that `DRY_RUN`
defaults to true; `tests/test_deploy_chart.py` drives the posture helpers in
`targets/openshift.sh` against real values files. The first real run against a namespace is the acceptance test, and
`DRY_RUN=true` is what makes it safe to take.

## Credentials

Bound by id, never written into a file here.

| Parameter | Kind | Used for |
| --- | --- | --- |
| `REGISTRY_CREDENTIALS_ID` | username/password | pushing images |
| `CLUSTER_CREDENTIALS_ID` | secret text | `oc login --token` |
| `DATABRICKS_CREDENTIALS_ID` | secret text | the workspace PAT (`DATABRICKS_TOKEN`) |

The application's own secrets are not Jenkins' business: the chart *names* them and an
`ExternalSecret`/`SealedSecret` populates them (`deploy/README.md`). A pipeline that carried
`CHEMCLAW_*` credentials would put every one of them in a build log's environment.
