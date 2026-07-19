# `workers/` — Temporal worker processes

**Responsibility:** the long-running processes that host workflow and activity
code and poll a task queue. One worker fleet per queue: heavy workers on
`hpc-jobs`, light workers on `background-jobs`. Workers are thin — they register
the workflows/activities from `workflows/` and connect using values from
`chemclaw.config`; they contain no business logic of their own.

Restarting a worker mid-job is the durability spike at CHECKMATE 1: the workflow
must resume from event history without re-running completed activities.

Empty until Phase 1 (plan steps 1.1, 1.8). Becomes a Python package when the
first worker entrypoint lands.
