"""Sending computed results outward: the result-sink seam and the record it publishes.

`publish.record` is the canonical shape a calculation takes on its way out — a subject, its
conditions, and typed facts about it. `publish.project` turns a calculator's own result model into
one; `publish.outbox` queues it durably; `publish.registry` is the seam a deployment attaches a
destination through, one `sinks/<name>/sink.yaml` folder plus its name in `CHEMCLAW_RESULT_SINKS`.

The counterpart of `ingest`, and deliberately built to the same template — a folder, a manifest, a
late-bound `module:callable`, an enable list. What separates them is direction, and therefore what
may go wrong: that seam reads, so its failure is a missing answer; this one writes, so its failure
is a record nobody has (D-2026-08-25).
"""
