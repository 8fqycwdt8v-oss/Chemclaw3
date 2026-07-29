# D-048 — F5: real HPC execution via a Nextflow launcher behind the QM activities (D-A5, D-A5a)

**Context.** The QM spine was mocked (a SLURM-style sleep). Its module docstring promised that
making compute real would touch *only* `workflows/activities.py`. F5 keeps that promise.

**Decision (D-A5a — launch interface).** The launcher is the **Seqera Platform / Tower REST API**:
run status is a plain GET, which survives a durable heartbeat-poll cleanly (no long-lived SSH
session to keep alive across worker restarts, unlike `nextflow` CLI over SSH; no bespoke internal
launcher to build). `workflows/hpc/nextflow.py` is that adapter — `launch_run` / `poll_run` /
`fetch_artifacts`, each taking an injectable httpx transport so the full launch→poll→fetch lifecycle
is proven offline against a fake endpoint.

**Decision (D-A5 — wiring).**
- `hpc_launch_interface` selects the backend inside the two QM activities: `"mock"` (default, kept
  for CI/local — no cluster) or `"nextflow"`. The activities are the *only* module changed; the
  workflow, the worker registration, and the agent are untouched — the mock's original promise held.
- The mock is retained verbatim behind the switch, so every existing durable test passes unchanged.
- `fetch_artifacts` returns the same `energy=… converged=…` text shape, so `parse_qm_output` is
  unchanged whether output came from the mock or a real run.
- **F5-T3 cache versioning:** `qm_job_key` folds in `hpc_pipeline_version` **only when set** — a
  pipeline bump becomes a cache miss (D-011/D-033), while the empty dev/mock version leaves keys
  byte-identical to before F5 (no orphaned cache, no test churn).
- **F5-T4 worker placement:** the `hpc-jobs` worker already registers the two activities by name;
  the real launcher therefore runs on that worker with no topology change — network reachability to
  the launcher is a deploy concern carried into F6.

**Deferred (noted, not silently dropped).** The cosmetic `QMJobWorkflow→CalculationWorkflow` /
`qm_job_key→calculation_key` rename (F5-T3, plan 1c.5): pure naming, high-churn across ids/tests, no
behavior change — deferred to avoid risk with no functional gain. Real `cclib` parsing of genuine QM
output replaces the regex parser once a real pipeline output format is fixed.

**Consequence.** The real Nextflow path is code-complete and lifecycle-tested offline; the mock keeps
CI cluster-free; a pipeline version is in the cache key. The only gated edge is a live cluster run.
