"""Sending computed results outward: the result-sink seam and the record it publishes.

`publish.record` is the canonical shape a calculation takes on its way out; `publish.project`
turns a calculator's result model into one; `publish.outbox` queues it durably;
`publish.registry` attaches a destination (one `sinks/<name>/sink.yaml` folder plus its name in
`CHEMCLAW_RESULT_SINKS`). Built to the same template as `ingest`, in the opposite direction:
that seam's failure is a missing answer, this one's is a record nobody has (D-2026-08-25).
"""
