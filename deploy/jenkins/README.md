# Jenkins delivery

GitHub Actions is the **gate**; Jenkins is the **delivery**. `.github/workflows/ci.yml` decides
whether a commit is allowed to exist (`make lint type cov`, the eleven validators, a chart render);
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
  "order": ["mcp-props", "mcp-calc", "core", "ui"],
  "components": {
    "mcp-props": {"kind": "deployment", "deployment": "chemclaw3-mcp-props", "container": "props",
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

**Digests, never tags.** `values.yaml` treats `image.digest` as the release knob and ignores
`image.tag` when it is set, because a tag is a pointer: `helm rollback` to a release naming `0.1.0`
fetches whatever `0.1.0` means now, and every audit record stamps a build revision that stops being
answerable the moment a tag is re-pushed (D-2026-08-01-a-tag-is-a-pointer-not-a-build, runbook
§(xiv)). Both targets refuse a value that is not a `sha256:` digest, and so does the release job.

**`kind` is `helm` for exactly one repository, and that is a real limitation rather than a design.**
Only `Chemclaw3` ships a chart. `Chemclaw3_ui` and each `Chemclaw3-mcp` server have an image and a
NetworkPolicy and no chart, so the honest minimum is `oc set image` against a Deployment an operator
created — it changes the bytes and claims nothing else. Charts for those two are a `BACKLOG.md` row.

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
files invoke exists, that the deploy path passes a digest and a stated egress posture, and that
`DRY_RUN` defaults to true. The first real run against a namespace is the acceptance test, and
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
