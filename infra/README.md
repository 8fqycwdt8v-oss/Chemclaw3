# `infra/` — local dev infrastructure

**Responsibility:** the developer-facing stack definition. `docker-compose.yml`
brings up a self-hosted Temporal dev server (frontend + web UI on :8081) and a
pgvector-enabled Postgres, started via `make up` / stopped via `make down`.

Ports and credentials mirror `.env.example` and `chemclaw/config.py`, so a fresh
checkout connects with no extra setup. This is a **dev** topology only — not a
production deployment (plan step 0.5).

`docker-compose.observability.yml` is a **second, separate stack**: one Phoenix
container (UI + REST on :6006, OTLP/gRPC on :4317), started via `make phoenix-up`.
It is the eval lane's reader — where an archived probe run is published so two
runs can be diffed (`make phoenix-publish`, AG-13) and where the live lane's
spans land. Separate from the file above because that one is what you need to run
anything and this one is opened deliberately to ask a question about a run; it
carries its own compose project name so `phoenix-down` cannot reach the spine's
Postgres. Not part of the Helm chart, and not meant to be — production traces go
to whatever collector the cluster runs.

`live/` holds the scripts the live-test lane is made of. `bootstrap.sh` provides
what `make up` provides on a machine with no Docker daemon (it defers to the
compose file whenever one is reachable), and records whether *it* created the
containers — so a lane that adopted the shared spine refuses to tear it down.
`processes.sh` starts and stops the connectors, the Temporal workers, the mock
gateway and the front door with readiness polls rather than sleeps. `soak.sh`
drives the lane in rounds, and `siblings.sh` is the one place that resolves a
sibling checkout's path for both lanes. All are reached through `make live-*`;
the procedure is in `docs/guides/runbook.md`.

**No lane here carries the compiled egress layer, and that is a real gap rather
than a detail.** `src/chemclaw/core/netguard_preload.c` is built in exactly one
place — `deploy/Containerfile`, through `deploy/build-netguard-preload.sh` — so
`make chat`, `make connectors`, `make live-up`, the four-repo `e2e-full-stack`
lane and every local or CI `pytest` run with it absent. The code is honest about
it (`chemclaw_egress_preload_armed` reads 0 and `netguard_preload.is_armed()`
asks the dynamic linker rather than an environment variable), but nothing in
these lanes exercises grpc's C-core or Temporal's Rust sdk-core against the
allowlist the way a pod does. What *does* exercise it is
`tests/test_netguard_preload.py`, which builds the library with the image's own
recipe and drives real clients through it; a green live lane is evidence about
the Python layer only (`D-2026-09-12-the-layer-that-binds-grpc-is-libc-not-socket-py`).

**Neither the script count nor the process list is written out here**, and the
count was wrong within one commit of being written — it said "the two scripts"
over four. What each process is called and which port it holds is a thing the
lane *knows at runtime* and prose does not: `bootstrap.sh status` prints it, and
`.live/run/<name>.port` is the authority for a port
(`D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose`).

`sql/` is the migration set `make db-migrate` applies, in filename order, against
a ledger with per-file checksums. The schema is forward-only and additive
(D-2026-08-04-the-schema-only-goes-forward), and one table per row is inventoried
in `sql/README.md`.

`sql/grants/` is deliberately **not** part of that set, and the runner's glob is
non-recursive so it cannot be swept in. Those files are `make db-grants`, which
re-runs on every deploy after the migrations: a grant is a reconciliation between
a schema that keeps growing and a runtime role that may be created at any time, so
run-once semantics would leave a later table ungranted and the application broken
on first use of it (D-2026-08-05-append-only-by-grant-not-by-contract). They no-op
where no `chemclaw_app` role exists, which is every dev database.
