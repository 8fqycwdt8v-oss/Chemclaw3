# Environment values

One Helm values file per environment — `dev.yaml`, `staging.yaml`, `prod.yaml` — passed to
`helm upgrade` by the release descriptor's `values` field.

**This folder ships empty on purpose.** Everything that belongs in these files is a fact about a
site rather than about this system: the route host, the Entra tenant and audience, the egress
destinations, the Postgres and Temporal addresses, the LLM endpoint (a Mosaic AI serving URL where
Databricks is the provider), the privileged role names, the replica counts. A plausible-looking
placeholder for any of them is a configuration that *looks* configured — it survives review, reaches
a cluster and grants or connects to nothing, which is the argument `deploy/README.md` already makes
about why the chart ships no placeholder role name.

Keep them here in the repository if they carry no secrets, or mount them from wherever your site
keeps environment configuration and point the descriptor's `values` at that path. Secrets do not
belong in either place: the chart names Secrets and an `ExternalSecret`/`SealedSecret` fills them.

Start from `deploy/helm/chemclaw/values.yaml`, which documents every key beside the argument for its
default, and set only what differs. Three that every environment must state:

- `networkPolicy.egressDestinations` (or `networkPolicy.allowAnyDestination: true`) — the chart
  refuses to render without one, because an empty list means *every* destination
  (`D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob`).
- `retention.windows` (or `retention.unboundedGrowthAccepted: true`) — the same guard on the other
  half of that decision: every `CHEMCLAW_RETENTION_*` window defaults to disabled, so a release that
  never states a posture runs with every durable table growing for the deployment's lifetime.
- `config.CHEMCLAW_ENTRA_PRIVILEGED_ROLES` — empty means every expensive job is refused for
  everyone, and nothing about the deployment looks wrong (`deploy/README.md`).

The pipeline has an escape hatch for each of the first two (`ALLOW_ANY_EGRESS_DESTINATION`,
`ACCEPT_UNBOUNDED_GROWTH`), and they exist so a first run is possible at all rather than as the
answer: both state a posture nobody chose, in a parameter that is not part of the release
descriptor. A values file here is the version a later reader can audit.
