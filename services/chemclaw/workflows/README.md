# `workflows/` — Temporal durable execution

**Responsibility:** the durable lifecycle of long, expensive jobs — QM/DFT on
HPC, and light background jobs (ELN sync, re-index, reports). Workflow code is
deterministic and replayable; all I/O and non-determinism lives in **activities**
(submit, poll with heartbeat, parse). Durability lives here and **only** here,
never in the MAF layer.

Two task queues (names come from `chemclaw.config`): `hpc-jobs` for the few heavy
workers, `background-jobs` for light ones (D-006). See `docs/architektur.md`
§2, §15.

Empty until Phase 1 (plan steps 1.1–1.4). Becomes a Python package when the first
workflow lands.
