# D-131 — The connector health probe follows the address override, instead of probing the pod itself

**Context.** Re-running Stage 5e's connector-kill scenario after D-130 produced a result that could
not be read: `/readyz` reported every connector `unreachable` **both before and after** the fleet
was SIGKILLed mid-turn. The scenario exists to prove the unreachable signal is loud (the failure
D-118 called out, where an agent silently runs with only its in-process tools), and it could not
distinguish a killed connector from one that was never probed correctly.

It was the latter, and not only in dev. `connectors/registry.py` applied the deployment's
`connector_urls` override to the connector's *tool* endpoint and nowhere else, while
`connectors/health.py` read `manifest.endpoint.health_url` straight off the file. A bundle's
manifest ships a loopback dev default, so the two disagreed the moment the override was set — and
**the shipped chart always sets it**: `chemclaw.connectorUrls` computes one in-cluster Service URL
per enabled bundle precisely so the front door does not have to be patched per environment.

The consequence in a cluster is that the front door probed `http://127.0.0.1:881x/healthz` — its own
pod, where nothing listens. Every connector read `unreachable` however healthy it was, so `/readyz`
and the `chemclaw_connectors_unhealthy` gauge were decorative; and under `connectors_required: true`
— the GxP fail-fast posture, the one a regulated deployment would pick — the probe raises at
startup, so the front door would have failed to start every time, with a message blaming connectors
that were fine.

**Decision.** One public `connectors.registry.health_url(manifest)`, and the probe goes through it.
The probe is a second caller of the override, so the override is what it must ask.

The move is a **suffix replacement, not an origin swap**, because the two deployments that exist put
a connector in different *places* rather than merely on different hosts:

| | endpoint | health |
|---|---|---|
| Helm (per-bundle Service) | `http://…-connector-chem:8814/mcp` | `…:8814/healthz` |
| `scripts.connectors_dev` (one port, mounted by name) | `http://127.0.0.1:8810/chem/mcp` | `…/chem/healthz` |

Keeping the health path verbatim is right for the first and wrong for the second — `…:8810/healthz`
is a 404 there, which is exactly why the dev topology never revealed the bug. So the manifest's own
two URLs define the relationship (whatever distinguishes its health URL from its endpoint URL), and
that difference is re-applied at the effective address. An override that does not end the way the
manifest's endpoint does falls back to the declared URL: possibly wrong, but not silently invented.

**Result, measured on the running stack** with the dev composite serving all six bundles:

| | before | after |
|---|---|---|
| `/readyz` with every connector healthy | `bo=unreachable, calc=unreachable, chem=unreachable, molfp=unreachable, rxnfp=unreachable, safety=unreachable` | `bo=healthy, calc=healthy, chem=healthy, molfp=healthy, rxnfp=healthy, safety=healthy` |
| `/readyz` after the fleet is killed mid-turn | (unchanged — indistinguishable) | all six flip to `unreachable` |

With the signal working, the scenario finally reports something: the turn whose connector died at
2.7 s still **answered**, retrying tools three times against a dead server and finishing in 39 s, and
the next turn completed on the reduced surface. Losing a connector costs capability, not the
conversation — which is what `connectors_required: false` promises and what had never actually been
observed end to end.

**Why no test caught it.** Every registry test asserted the override on the tool URL
(`test_connector_urls_override_the_manifest_address`) and no test asserted anything about the probe
URL at all, so the two halves of one address were covered asymmetrically. `tests/test_deploy_chart.py`
checks that the chart *computes* the URLs; nothing checked that everything reading an address goes
through the same function. Three tests now pin it, including the path-moving case that the naive
origin swap would fail.

---
