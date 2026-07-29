# D-006 — One execution system: Temporal task queues, no pg-boss

Since Temporal already runs for HPC jobs, small async jobs (ELN sync, re-index, notifications,
reports) use a separate `background-jobs` task queue instead of a second queue system. §12.1.
