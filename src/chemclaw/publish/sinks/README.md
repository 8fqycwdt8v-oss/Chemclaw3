# `publish/sinks` — the shipped `sink.yaml` manifests

One folder per sink, discovered by `publish.registry` the way `connectors/` and `ingest/sources/`
are discovered — and **enabled by nobody** unless `CHEMCLAW_RESULT_SINKS` names it, because
publishing sends computed results to a database this system does not own.

`postgres/` is the one shipped manifest. It describes how to *reach* a store, not what its tables
are called: the schema here is ours (`schema/result-store/`), which is the one way this seam differs
from the warehouse ELN binding, where the site's schema is the manifest.
