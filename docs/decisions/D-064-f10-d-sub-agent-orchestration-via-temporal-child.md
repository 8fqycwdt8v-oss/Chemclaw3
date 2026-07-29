# D-064 — F10-D: sub-agent orchestration via Temporal child workflows (D-A13)

**Context.** Report-section retrieval and memory synthesis each fanned a task into independent steps
but ran them in one monolithic activity, so a single poison item failed the whole batch and there was
no per-step durability. A generic child-workflow fan-out was justified by these *two* real callers
(Rule of Three), not speculatively.

**Decision.** `workflows/orchestrator.py::fan_out(child, inputs, *, id_prefix, ...)` runs each input
as a child workflow with bounded concurrency (fixed-size batches — deterministic under replay),
per-child retry, and D-030 isolation (a child that exhausts its retries is logged and dropped, its
siblings unaffected, successful results in input order). Adopted by `ReportSectionWorkflow` (one per
section) and — after extracting the pure `build_*_notes` in `memory/jobs.py` — a shared
`PublishNoteWorkflow` (one per memory note). Orchestration stays a Temporal-layer concern; MAF remains
the single conversational agent. The conversational multi-agent mesh stays gated (trigger recorded).

**Consequence.** Report + memory synthesis now run as exactly-once child workflows with per-child
retry and worker-restart durability. Section/group logic is unchanged (still PR-gated, still cited);
only the execution topology gained parallelism + isolation. Config
`orchestrator_max_parallel_children` (default 8).

**Result.** `make lint type test` green (the Temporal-env fan-out test runs in CI, skips offline).
Tests: `test_orchestrator`, `test_memory` (builder behavior-preserving), `test_report_workflow` /
`test_workers` registration, `test_config`.
