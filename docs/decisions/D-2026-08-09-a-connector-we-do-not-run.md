# D-2026-08-09-a-connector-we-do-not-run — hosting is a deployment fact, and the URL is the whole knob

**Status:** accepted

## Context

The question that prompted this was ordinary: *a new model is available behind a FastAPI MCP
server — what do I have to do to make it available in ChemClaw?* The connector seam answers almost
all of it. A capability is a folder with a `connector.yaml` (D-118/D-120), `HttpEndpoint` is
described in its own docstring as "a connector reached over MCP streamable-HTTP — the normal case
(its own FastAPI server)", `health_url` is optional *because* "a third-party MCP server may expose
nothing", and `connector_urls` exists so a deployment can move a bundle's address without patching
the repo. Discovery is filesystem-based, so registering the bundle is genuinely one file.

The Helm chart is where the answer stopped being true. Every path in it assumes that a bundle with
an `endpoint:` is a pod built from *our* image:

- `chemclaw.connectorUrls` computed `http://<release>-connector-<name>:8080/mcp` for every
  `enabled && server` entry, and that computed value **wins** over the manifest's own URL
  (`connectors/registry.py::_endpoint_url`). A bundle naming somebody else's host would have been
  dialled at an in-cluster Service instead.
- `deployment-connectors.yaml` rendered a Deployment running `uvicorn
  connectors.<name>.server.app:app` — a module an external model does not have — plus a Service
  selecting pods that would never appear.
- `tests/test_deploy_chart.py` pins `server` to "the manifest declares an endpoint", **both ways**,
  so the bundle could not opt out by setting `server: false` without breaking that mirror.

So there was no shape in `values.yaml` for *enabled, endpoint-bearing, not ours to run*, and the
failure mode was silent in the worst way: an unreachable connector degrades rather than erroring
(`connectors/transport.py`), so the model simply never sees those tools and reasons from what
remains. The operator sees a capability that is quietly missing from every turn.

## Decision

**One knob, and it is the address itself: `connectors.<name>.url` in the chart's values.** Setting
it says the bundle's MCP server is hosted outside this release, and gives where. That bundle then
gets no app Deployment and no Service, and `chemclaw.connectorUrls` emits the given address instead
of a computed one. `chemclaw.pooledProcesses` stops counting a server process for it.

Three things follow from that, each deliberate:

**Hosting is a deployment fact, so it lives in values and not in the manifest.** The same bundle can
be a local process in dev, our pod in staging, and a platform team's endpoint in production. None of
that is what the capability *is*, which is all a manifest is allowed to say (D-118/D-120). This is
the same argument that put `connector_urls` in config rather than in `connector.yaml`, applied one
level out: the chart already treats *where* a connector is as the deployment's business, and this
extends that to *whose it is*.

**The knob is the URL rather than an `external: true` beside it.** A boolean and an address are two
declarations of one fact that can disagree — `external: true` with no URL, or a URL with the flag
forgotten — and the chart has already learned this lesson twice: `connectorUrls` and
`pooledProcesses` are both *computed* from the topology block precisely because a hand-maintained
second copy went stale. Presence of the address is the flag.

**`server:` keeps meaning "the manifest declares an endpoint", and the worker is untouched.** The
mirror test that pins `server` both ways against the manifest is unchanged, because it is checking a
different question from the one `url` answers. And a bundle's durable jobs run on our own Temporal
queue whoever serves its tools, so an external endpoint must not take its worker Deployment away.

### The claim that was not enforced

`NoAuth`'s docstring stated that `ConnectorManifest` refuses `auth: mode: none` for a non-loopback
URL. **No such validator existed anywhere in the tree.** The rule was written in the docstring, and
nowhere else — three sentences of prose about a check that had never been implemented.

It cost nothing while every bundle was ours and shipped a loopback dev default. It stops being free
the moment a manifest can name somebody else's host, which is exactly what this ADR enables: an
unauthenticated MCP call carries the turn's actor and full role set (`connectors/identity.py`) to a
host outside our trust boundary. So the rule is now real, on `HttpEndpoint`, where the URL and the
auth mode are both in scope.

**It checks the declared URL, not the effective one after `connector_urls`.** A deployment override
points at the operator's own infrastructure — in the shipped chart, an in-cluster Service bounded by
the `connector-ingress` NetworkPolicy — and a rule that failed on those would flag the entire
shipped fleet the day the chart set the override. A gate that fires on the normal case is a gate
people switch off. What it catches is the case with no compensating control and no prior way to
express it: a manifest in the repo naming a network host with no credential on the call.

The loopback predicate moved to `core/http.py` and is now shared with the front door's bind rule
(SEC-2), which had the only copy. Two safety rules asking "is this address reachable from the
network" must not be able to answer differently; `connectors -> api` is an edge the layering policy
explicitly removed, so `core` is where the shared answer lives.

## Consequences

- Attaching an externally hosted model server is: a `connector.yaml` with an `endpoint:` (bearer
  auth, since it is not loopback), a `values.yaml` entry with `url:`, and the host added to
  `networkPolicy.egressDestinations`. No core Python, no new image, no pods.
- A future edit that reverts a `server` block to a bare `if $cfg.server` is caught: the test asserts
  the *absence* of the unguarded form, not merely the presence of the guarded one.
- The offline suite pins the template text and mirrors both helpers in Python, which is the pattern
  `tests/test_helm_chart.py` already uses — `make test` has no `helm`. The rendered proof is
  `make helm-validate`, a separate CI job. **The render was not executed for this change**: this
  environment's proxy denies `get.helm.sh` and helm publishes no GitHub release asset, so the
  guards are pinned by test and by review, and CI's helm job is what renders them.
- `auth: mode: none` on a non-loopback URL now fails at manifest load rather than at `make
  connector-validate`, so it is caught by anything that touches the registry. Every shipped bundle
  declares a loopback default and is unaffected.
