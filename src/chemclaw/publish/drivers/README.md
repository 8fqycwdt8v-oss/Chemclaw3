# `publish/drivers` — the shipped result-sink drivers

Two, deliberately. A `ResultSink` Protocol with a single implementation would be an abstraction with
one caller, which this codebase inlines on sight; two real ones is what makes the seam earn itself,
and they cover the two shapes a site's results store actually takes.

- `sql.py` / `postgres.py` — a database a DBA runs our DDL on (`schema/result-store/`), named by
  `publish/sinks/postgres/sink.yaml`.
- `http.py` — a service that accepts a document. Named in no shipped `sink.yaml`, and **not dead**:
  a site names it in its own, which is the "a deployment selects it" half of the rule
  (`D-2026-08-27-a-hold-nothing-can-open-is-not-a-hold` keeps it by name).

A driver is reached as a `module:callable` out of a manifest, so a Python-only scan of this package
will report every file here as unreferenced. That is the trap this repository's seam design sets for
a dead-code audit, and it is worth knowing before deleting anything.
