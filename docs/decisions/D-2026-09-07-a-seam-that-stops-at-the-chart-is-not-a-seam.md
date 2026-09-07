# D-2026-09-07-a-seam-that-stops-at-the-chart-is-not-a-seam — the fleet's addresses, and the connector seam on OpenShift

## Status

Accepted. Two findings from the wave-10 review of `deploy/helm/chemclaw/`, kept in one ADR because
they are the same shape: a statement this repository makes about its relationship to
`Chemclaw3-mcp`, believed because it is written down, and false on the only stack that ships.

## Context — 1. five hostnames that resolve to nothing

`values.yaml` addressed the sibling fleet at `chemclaw3-mcp-<server>`:

```
301:    url: http://chemclaw3-mcp-safety:8859/mcp
321:    url: http://chemclaw3-mcp-chem:8858/mcp
335:    url: http://chemclaw3-mcp-rxnpredict:8857/mcp
785:  CHEMCLAW_CALC_SERVER_URL: "http://chemclaw3-mcp-calc:8860/mcp"
837:  CHEMCLAW_RXNLABEL_SERVER_URL: "http://chemclaw3-mcp-rxnlabel:8865/mcp"
```

The Services that repository creates are `chemclaw-mcp-<server>` — measured off
`servers/*/deploy/service.yaml`, all seven, `calc` `chem` `props` `pyexec` `rxnlabel` `rxnpredict`
`safety`. Nothing on that side prefixes or rewrites them: the fleet ships no `Chart.yaml` and no
kustomization, so `kubectl apply` puts exactly `metadata.name` in the namespace. Every port agrees
(8857/8858/8859/8860/8865); only the names differ, by one character.

**The values file stated the correct rule directly above the name that broke it.** The comment on
`CHEMCLAW_CALC_SERVER_URL` read: *"Named `chemclaw3-mcp-calc` for the same reason
`connectors.chem.url` / `connectors.safety.url` name their siblings: whatever Service the sibling
repo's chart gives its `calc` server in this namespace."* The Service the sibling gives it is
`chemclaw-mcp-calc`. The name was wrong by its own definition — which is what makes this a
measurement rather than a matter of taste, and what makes prose the wrong instrument: a comment
restating a rule cannot check a value against it.

The consequences are not equal. `chem`, `safety` and `rxnpredict` degrade — the connector fails its
health probe, `chemclaw_connectors_unhealthy` counts it, its tools are unreachable.
`CHEMCLAW_CALC_SERVER_URL` is what `connectors/calc/remote.py::calc_session` dials for **every**
calculation this system performs, and since `D-2026-08-26-semiempirical-is-the-whole-tier` there is
no second tier to fall back to. That failure is silent by the file's own account: the pod's
`/healthz` never touches the calculation server, so the probes stay green.

### The bare short name is right, and that was measured too

The sibling's `docs/integration.md` writes the example address namespace-qualified
(`chemclaw-mcp-props.chemclaw-tools.svc`), which raises the obvious question of whether this chart's
bare names are a second, latent defect. They are not, and the reason is on the fleet's side: every
`servers/*/deploy/networkpolicy.yaml` admits its caller with a **bare `podSelector`**
(`app.kubernetes.io/name: chemclaw`), which in a NetworkPolicy means *the policy's own namespace*.
This chart labels every pod `app.kubernetes.io/name: chemclaw` (`_helpers.tpl`), so a same-namespace
deployment is admitted and a cross-namespace one is not — a qualified address would resolve in DNS
and then be dropped by the server's own ingress rule. Cross-namespace is a change on that side
first. The shipped, working shape is the fleet beside us, addressed short.

## Context — 2. the D-120 seam stops at the chart

D-120 states the connector seam as "one `connector.yaml` folder plus its name in
`CHEMCLAW_CONNECTORS_DIR`, with **zero** core edits", and the fleet's `manifests/README.md` states
it from the other side: "registering this whole fleet is one environment variable and no code
change on either side". Both are true of `make connectors`. Measured against the chart, on
2026-09-07, the seam is four declarations and **two of them did not exist**:

| what a third-party bundle needs | shipped chart |
| --- | --- |
| an address and enablement (`connectors.<name>.url`) | ✅ `chemclaw.connectorUrls` / `connectorsEnabled` `range` the map — an operator's own key renders |
| a bearer (`secrets.optionalKeys.<name>Token`) | ✅ `_helpers.tpl` `range`s that map too |
| an egress port (`networkPolicy.egressPorts.<name>`) | ❌ the rule hand-named six keys of the map |
| its manifest on `CHEMCLAW_CONNECTORS_DIR` | ❌ no value, no volume, no mount, anywhere in the chart |

Rendered with `--set connectors.props.… --set networkPolicy.egressPorts.props=8850 --set
secrets.optionalKeys.propsToken=…`: `props` appears in `CHEMCLAW_CONNECTOR_URLS` and
`CHEMCLAW_CONNECTORS_ENABLED`, its token is referenced 36 times — and `grep -c 'port: 8850'`
returns **0**, and `grep -c CHEMCLAW_CONNECTORS_DIR` returns **0**.

So `Chemclaw3-mcp`'s `props` and `pyexec`, two real capabilities with images, Services,
NetworkPolicies and published manifests, were unreachable from an OpenShift release by any means
short of editing the chart.

**And the second failure is not a quiet absence.** Because `CHEMCLAW_CONNECTORS_ENABLED` is derived
from the same `connectors:` block, an operator who follows D-120 gets `props` into that list with no
bundle behind it, and `registry.enabled()` refuses that by design:

```
ConnectorError: connectors_enabled names unknown connector(s) ['props'];
discovered: ['bo', 'calc', 'chem', 'molfp', 'results', 'rxnfp', 'rxnpredict', 'safety']
```

raised at import, in every pod at once — the seam's own loud guard, reached through a door the chart
had left shut.

## Decision

**1. The fleet addresses are corrected, and checked against the fleet rather than against prose.**
All five values name the Service the sibling creates. The comments that stated the rule now name
the file that decides it (`servers/<name>/deploy/service.yaml`) and record what the bare short name
means. `tests/test_helm_chart.py::test_every_fleet_address_names_a_service_the_sibling_actually_creates`
reads those manifests out of a sibling checkout and compares name and port against every fleet
address anywhere in `values.yaml`. The address set is found by pattern — `chemclaw<digits?>-mcp-` —
deliberately loosely, so a *wrong* spelling is picked up and checked rather than falling out of the
set it is wrong about; anchoring on the correct name would have found nothing to complain about.
Without a checkout it **skips, naming all five addresses it therefore did not check**, which is this
repository's established shape for a cross-repo control (`tests/test_context_floor.py`'s allowance
for the schemas it cannot serve). The checkout is resolved through `tests/siblings.py`, which asks
`infra/live/siblings.sh` — one search, four candidate roots, both casings, `CHEMCLAW_MCP_REPO` —
rather than adding a fifth guess.

**2. Position (a): the seam is completed on the chart, not documented away.** The alternative
considered was to state plainly in `values.yaml` and `deploy/README.md` that a third-party bundle
requires a chart edit, and stop claiming zero core edits for the OpenShift path. It was rejected on
the measurement above: *two* of the four declarations already worked map-driven, so that prose would
itself have been a new inaccuracy unless it enumerated which halves work — and the two that did not
were one template generalisation and one values block, both smaller than the paragraph that would
have explained why they were absent. A seam that is 50% real is not a documentation problem.

- `networkPolicy.egressPorts` is **ranged** rather than hand-named. Six keys of a map were emitted
  one `- protocol: TCP` at a time, so a key the chart's own values did not already carry a line for
  was accepted and rendered nothing — and a NetworkPolicy drop is silent.
- `extraConnectors.bundles` is a list of ConfigMaps, each mounted read-only at
  `extraConnectors.mountPath/<name>`, with `CHEMCLAW_CONNECTORS_DIR` rendered as
  `mountPath:shippedPath` **only when the list is non-empty** — setting it at all replaces the
  code's default outright (`connectors_dir` is a plain pathsep list, not an extension point), so an
  unconditional value would restate the image's layout on every release including the ones that
  mount nothing. A ConfigMap rather than a generic `extraVolumes` passthrough because it is the
  route the sibling's `docs/integration.md` already documents, and because a blank cheque is not a
  seam either.
- The mount is **uniform across every component that resolves the registry**, not placed on the one
  that "needs" it: the variable is set once in a ConfigMap every pod reads, and `_bundle_dirs`
  skips a missing directory in silence while `enabled()` raises on a name it cannot discover — so a
  pod that got the variable and not the mount does not serve one bundle fewer, it crash-loops. The
  connector *server* Deployment, which declared no volume at all, gets one for the first time.

**Two exemptions, measured rather than assumed.** The knowledge-sync containers run a shell script
that constructs no `Settings`. The `migrate` and `convert` hook Jobs never import
`chemclaw.connectors.registry` at all — checked by importing both entrypoints under
`CHEMCLAW_CONNECTORS_ENABLED=molfp:props` and finding the module absent from `sys.modules` — and
exempting them matters in the other direction too: `migrate` is a `pre-install` hook, and a hook Job
mounting an operator-supplied ConfigMap cannot start until that object exists, which would make a
first install fail on a directory it never reads. The test asserts the exempt set is *exactly* those
two, so a third component quietly losing the mount is a failure rather than a widening.

## Consequences

- Five addresses change. A deployment that had worked around the old names by creating aliasing
  Services in its namespace will find the alias unused; nothing breaks, but the alias is now dead.
- `extraConnectors.shippedPath` is a second statement of the image's layout, and it is treated as
  one: `test_the_shipped_connector_path_is_the_path_the_image_has` derives it from the
  Containerfile's `WORKDIR` and `COPY src ./src` plus this package's own location, so an image
  rebuild that moves the tree fails here rather than in a cluster. That is the quiet direction of
  this whole finding — a wrong `shippedPath` mounts the new bundle and loses every shipped one, then
  fails loudly on `connectors_enabled` naming a bundle that *is* mounted as the one it cannot find.
- **Overriding a shipped bundle is now reachable from a values file.** Earlier directories win a
  name collision, and the mounted directory comes first — which the fleet's `manifests/` uses on
  purpose for `chem` and `safety`, both complete ports carrying their bundle's name. Stated in
  `values.yaml` beside `mountPath` rather than left to be discovered from the ordering.
- Three assertions in `tests/test_helm_chart.py` that asked `f"egressPorts.{key}" in <template
  text>` are replaced by two rendered ones in `tests/test_deploy_chart.py`. The text form answered
  "did somebody write a line naming this key", which stops meaning anything once the rule ranges the
  map, and never reached a key an operator adds themselves.
- What this does **not** change: an externally hosted bundle still owes its operator an
  `egressDestinations` entry and a credential, exactly as `chem`, `safety` and `rxnpredict` already
  do. The claim now true on the target stack is D-120's — a bundle is a directory and a name, with
  no edit to this repository — not that hosting somebody else's server needs no configuration.
