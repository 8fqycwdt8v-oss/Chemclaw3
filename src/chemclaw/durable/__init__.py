"""Temporal durable-execution layer: workflow definitions and their activities.

Workflow code (`qm_job`) is deterministic and replayable; all I/O and
non-determinism lives in `activities`. Durability lives here and only here — the
conversation layer starts these workflows but holds no durable state for them (D-002).
"""
