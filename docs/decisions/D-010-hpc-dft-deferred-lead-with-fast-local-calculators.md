# D-010 — HPC/DFT deferred; lead with fast local calculators (user decision)

The real HPC/SLURM DFT path is postponed. The mock spine (Phase 1) already proves the durable
async pattern, so early value comes from **fast, locally runnable** compute instead: semiempirical
**xTB (latest GFN, GFN2)** and ML predictors (**GNN solubility**, **pKa/property**). They reuse the
identical Temporal durability pattern; only the heavy HPC/DFT backend is wired later, when that
accuracy is actually needed and HPC access exists. Plan Phase 1c; DEFERRED.md row for HPC/DFT.
