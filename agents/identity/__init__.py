"""Entra identity bridges for backend workloads (plan Phase F4).

Three narrow, independently-adoptable pieces that let backend components carry a real Entra
identity without a stored client secret:

- `workload` — a pod mints its *own* service token via Workload Identity Federation (F4-T2).
- `obo` — a user-scoped call exchanges the user's token On-Behalf-Of for a downstream token (F4-T4).
- `hpc_bridge` — maps an Entra `oid` to the HPC/Nextflow service identity, logging every mapping
  (F4-T6), since HPC is one of the two non-Entra transports (§7.2).

The generic LLM credential (`agents/llm_provider.py`) is the one documented exception and uses
none of these.
"""
