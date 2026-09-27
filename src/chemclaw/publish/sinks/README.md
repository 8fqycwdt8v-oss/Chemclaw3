# `publish/sinks` — the shipped `sink.yaml` manifests

One folder per sink, discovered by `publish.registry` the way `connectors/` and `ingest/sources/`
are discovered — and **enabled by nobody** unless `CHEMCLAW_RESULT_SINKS` names it, because
publishing sends computed results to a database this system does not own.

`postgres/` is the one shipped manifest. It describes how to *reach* a store, not what its tables
are called: the schema here is ours (`schema/result-store/`), which is the one way this seam differs
from the warehouse ELN binding, where the site's schema is the manifest.

## The second shipped driver has no shipped manifest, and this is how it is named

`publish/drivers/http.py` is the other driver this repository ships, and
`D-2026-08-27-a-hold-nothing-can-open-is-not-a-hold` keeps it under the rule "a thing no
*configuration* can reach is dead, a thing a *deployment* selects is not". That rule was being
asserted rather than met: the `module:callable` a site has to write —
`chemclaw.publish.drivers.http:HttpResultSink` — appeared in no manifest, README or document in
this tree, so selecting it meant reading the driver's `__init__` signature out of the source. A
driver a deployment cannot name is not one a deployment selects.

It ships no folder of its own **because it has nothing to ship**: `url` is required and has
deliberately no default, so a manifest here would carry a fabricated endpoint that
`CHEMCLAW_RESULT_SINKS` could enable. A site writes its own folder instead and puts it first on
`CHEMCLAW_RESULT_SINKS_DIR`, exactly as it would to override `postgres/`:

```yaml
# <your-dir>/lims/sink.yaml
name: lims                       # must equal the folder name; the token in CHEMCLAW_RESULT_SINKS
description: The site LIMS results endpoint.
driver: chemclaw.publish.drivers.http:HttpResultSink
config:                          # the driver's own keyword arguments — its signature is the schema
  url: https://lims.internal/api/results     # required; no default
  token_env: LIMS_TOKEN          # the variable NAME, never the value; read per request
  timeout_seconds: 30.0
```

`name` and `tenant_id` are supplied by the registry and are refused inside `config:`. `verify_tls`
is settable and must not be set false — an unverified TLS connection to a results store is an
unauthenticated one — and a non-loopback `http://` URL is refused outright under `entra_required`.
`make sink-validate` binds `config:` against the driver's signature, so a wrong key fails in CI.
