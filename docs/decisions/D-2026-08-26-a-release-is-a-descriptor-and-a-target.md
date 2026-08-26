# D-2026-08-26-a-release-is-a-descriptor-and-a-target — Jenkins delivery across four repositories

## Status

Accepted, and **unrun**. Every pipeline file this ADR describes is written against a cluster,
registry and workspace that this environment does not have. That is stated here rather than
discovered later, because a pipeline is not evidence about somebody else's infrastructure and this
repository has a long record of controls that existed, were described in the present tense, and were
never exercised.

## Context

Four repositories make one system — `Chemclaw3` (orchestration, the chart), `Chemclaw3-mcp` (seven
MCP servers), `Chemclaw3_ui` (BFF + SPA), `Chemclaw3_mock` (a test double for the integrations a
site has not attached yet) — and none of them can currently be *delivered* anywhere.

What exists is a gate. `.github/workflows/ci.yml` runs `make lint type cov` against a real Postgres
plus eleven validators; `image.yml` builds the image and smoke-imports every component as a non-root
UID; `Chemclaw3_ui` builds and exercises its container; `Chemclaw3-mcp` runs its suite with the
network taken away. Three facts about that estate decided this ADR:

1. **Nothing pushes an image anywhere.** All four builds are thrown away when the job ends.
   `docs/planning/DEFERRED.md` has carried "Push-to-registry + `helm upgrade` rollout in CI" since
   D-117 deleted the stub job whose entire body was an `echo`.
2. **`Chemclaw3-mcp` builds no image at all.** Seven `Containerfile`s, exercised by nothing.
   `Chemclaw3_mock` has no CI of any kind and no image.
3. **Only `Chemclaw3` ships a chart.** The other three have images (or not) and no deployable
   description of themselves.

The target estate was then named: Jenkins for delivery with GitHub Actions kept as the gate,
`helm upgrade --install` run directly rather than through GitOps, and **both OpenShift and
Databricks viable as deployment targets** — with Databricks additionally carrying three of this
system's dependencies (the ELN warehouse behind `ingest/sources/eln-databricks`, an OpenAI-compatible
Mosaic AI serving endpoint behind the F0 provider seam, and heavy compute beside the Nextflow/HPC
launcher).

## Decision

**A release is a *descriptor* — a set of image digests — and a *target* — a script that applies it.**
Everything else follows from that sentence.

### 1. Jenkins delivers; GitHub Actions gates

No stage here re-runs `make ci` by default. A second implementation of the gate, in Groovy,
maintained by hand, is a second answer to the same question. `RUN_GATE` exists and defaults to
**false** for an air-gapped, Jenkins-only estate — because a pipeline that gates nothing and a
pipeline that deliberately gates elsewhere look identical from the outside, and the parameter is
what tells them apart.

### 2. The descriptor names digests, never tags

`deploy/helm/chemclaw/values.yaml` already treats `image.digest` as the release knob and ignores
`image.tag` when it is set (D-2026-08-01-a-tag-is-a-pointer-not-a-build): a tag is a pointer, so
`helm rollback` to a release naming `0.1.0` fetches whatever `0.1.0` means now, and the build
revision stamped onto every audit record stops being answerable. The pipelines therefore *return*
the digest the registry assigned from the build, refuse anything that is not `sha256:…`, and pass it
to the chart. `build_and_push` in `deploy/jenkins/lib/image.sh` exists mainly to make that return
value uniform across builders.

Four repositories publishing independently is also why the descriptor is a file rather than a set of
parameters: "what is in staging" has to be one reviewable, archived object, not four build numbers
someone correlates by timestamp.

### 3. The builder is a parameter, because the agent estate is not knowable from a repository

OpenShift will not give an agent a Docker socket; a VM agent usually has one; a pod agent normally
gets buildah or kaniko. All four produce the same bytes from the same Containerfile and none of them
agree on how you ask, so one function knows the four dialects and the pipelines name what they want
built. This is the one place where "keep it generic" earned an abstraction: there are four real
callers, not a hypothetical second one.

### 4. Both targets read the same descriptor and each skips what the other owns

A real environment is split rather than exclusive: the services on OpenShift, the data and compute on
Databricks. So `targets/openshift.sh` and `targets/databricks.sh` take the same file, and a component
of a kind the other target owns is skipped with a log line rather than an error.

`targets/databricks.sh` deploys asset bundles and apps, and **preflights what a release consumes but
does not create** — the serving endpoint and the SQL warehouse. That check is not decoration: its
absence fails in the quietest possible way, because a front door with an unreachable model endpoint
starts, passes both probes and dies at the first turn. `/readyz` probes connectors and knows nothing
about Databricks.

What the script does **not** do is pretend Databricks hosts Postgres, Temporal or the worker fleet.

### 5. `oc set image` where there is no chart, and it is named as a limitation

`Chemclaw3_ui` and each `Chemclaw3-mcp` server have an image and a NetworkPolicy and no chart. The
honest minimum is `oc set image` against a Deployment an operator created: it changes the bytes and
claims nothing else. Writing a chart for either from here would be inventing somebody's Service,
Route and resource limits. Both are `BACKLOG.md` rows.

### 6. Order is a dependency, not alphabetical, and the mock is never promoted

The fleet comes up before the core that dials it: under `CHEMCLAW_CONNECTORS_REQUIRED=true` an
unreachable connector is a hard startup failure of the front door, so the reverse order turns a
rollout into a crash-loop that reads as a core defect. The UI comes last, being useless before the
API it proxies answers.

`Chemclaw3_mock` is absent from every environment above `dev`. It is a stand-in HPC launcher, ELN
source, Entra tenant and vendor MCP tool; deployed beside the real integrations it would give the
system two answers to the same question. Its pipeline gates it — which it has never had — and the dev
e2e lane runs it. Nothing promotes it.

### 7. `DRY_RUN` defaults to true

A delivery pipeline's first run happens against a real namespace. `helm --dry-run` and
`--dry-run=server` are what make that first run safe to take, so they are the default and the
mutating run is the deliberate one.

## Consequences

- The `DEFERRED.md` row survives with its *reason* rewritten: the rollout is no longer unwritten, it
  is unrun, and its trigger is now credentials rather than a design.
- `tests/test_jenkins_delivery.py` checks the half a file can check — that every `make` target the
  pipelines invoke exists, that every script they call exists and parses, that the deploy path pins a
  digest, that a release must state an egress posture, and that `DRY_RUN` defaults to true. It
  deliberately asserts nothing about whether any of it works against a cluster.
- Environment values ship **empty** (`deploy/jenkins/environments/README.md`) for the same reason the
  chart ships no placeholder role name: a plausible-looking value is a configuration that looks
  configured, survives review, reaches a cluster and connects to nothing.
- The four repositories each gained a `Jenkinsfile` and none gained a dependency on a Jenkins shared
  library. A shared library would be a fifth repository to version, review and pin, to save
  duplication that is mostly stage *names* — the four pipelines build genuinely different artifacts.

## Alternatives considered

**GitOps (Argo CD / Flux) instead of a direct `helm upgrade`.** Jenkins would push images and commit a
digest bump, and never hold cluster credentials — which is the better posture. Not chosen here
because it was not the estate described, and because it needs a values repository that does not
exist. The descriptor makes the switch cheap if it comes: a GitOps target is a third script that
writes the same digests into a commit instead of into a cluster.

**Jenkins mirroring the whole gate.** Rejected: two implementations of `make ci` disagree eventually,
and the one that disagrees quietly is the one nobody watches.

**Databricks hosting the services.** Databricks Apps can host a FastAPI process, but Postgres,
Temporal and a worker fleet polling durable queues are not workspace-shaped, and D-002's rule that
durability lives only in Temporal is not something a compute platform substitutes for. Databricks
carries the three dependencies it genuinely carries, and the services run where services run.
