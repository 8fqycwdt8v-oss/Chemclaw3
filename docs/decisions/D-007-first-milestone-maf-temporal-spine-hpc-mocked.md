# D-007 — First milestone: MAF + Temporal spine (HPC mocked)

Prove the async, durable job path end-to-end before building the rest; everything else hangs
off this pattern. `submit_to_hpc` is mocked so durability is testable without SLURM. Plan Phase 1.
