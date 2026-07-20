# DEFERRED

Consciously postponed items — each with the reason it is *not now* and the trigger that would
revisit it. Default is "off-the-shelf, defer until measured".

| Item | Why not now | Trigger to revisit |
|---|---|---|
| **HPC/DFT real integration** (SLURM, `submit_to_hpc`) | User deferred it; the mock spine proves the durable pattern and the early compute (xTB/GFN2 + ML predictors, Phase 1c) covers near-term needs locally | Heavy QM/DFT accuracy is genuinely required **and** HPC access is provisioned |
| Postgres RLS mirror of the graph | Broad internal read access is fine for cross-project learning; a mirror adds a sync pipeline + a second source of truth | Real, combinatorial project-level confidentiality requirements |
| `knowledge/` as its own Git repo | A subfolder is enough for v1 | Governance/confidentiality boundary requires repo split |
| Second queue system (pg-boss) | Temporal already runs; a second task queue covers small jobs | — (decided against, see D-006) |
| MAF Durable Extension for jobs | Temporal owns durability; Azure-Functions-native and job-inappropriate | Only very long *conversation* pauses (days awaiting human input) |
| Universal ELN abstraction | Individuality of each ELN can't be abstracted away up front | At the third real ELN source, generalize shared adapter bits |
| External literature/patent retrievers | Internal knowledge+data harness comes first | After Phase 5b core; add as one more retriever behind the same interface |
| Tabular Foundation Model (TabPFN) tool | Nice screening step, not on the critical path | When numeric trend prediction is needed; check the model version's license first |
| ~~xTB semiempirical~~ | **Pulled forward → Phase 1c** (now a primary early calculator, GFN2), not a DFT pre-filter | — (decided in, D-010) |
| Retrosynthesis + reaction prediction | Not on the spine-first critical path; big domain module | After the spine + graph + fingerprint layers exist; when route planning is a real user need |
| ~~DoE / Bayesian optimization~~ | **Pulled forward → Phase 1d** (BoFire as BO engine), drives which calculations are worth running | — (decided in, D-012) |
| Lab automation / SiLA2 closed-loop | Requires real instrument integration; out of v1 scope | When physical/robotic execution enters scope |
| Process flowsheet synthesis/simulation | Separate capability area (e.g. Aspen HYSYS) | When process-design (not just reaction) is in scope |
| Multimodal analytical data (spectra/images) | Adds modality-specific ingestion complexity | When analytical raw data beyond structured fields is needed |
| Domain foundation models | Heavy; general LLM + tools suffices for v1 | When task accuracy plateaus and a domain model is justified |
| Sub-quadratic playbook clustering | `memory/playbook.py` pairwise Tanimoto is O(n²) — fine for the current corpus, and simple/exact | ~10⁴ reactions (~10⁸ comparisons per run); switch to per-reaction Postgres HNSW k-NN |
| Per-key in-flight dedup in the calc store | Two *concurrent* misses on one key both compute (benign last-writer-wins upsert); serializing needs cross-process locking | Duplicate expensive runs (real HPC/DFT) become measurable cost |
| Wire the ORD adapter into the durable `ElnSyncWorkflow` | The workflow runs the one real source (`JsonExportAdapter`); `OrdJsonAdapter` satisfies the same `ElnAdapter` contract and is proven through `sync_entries`, but no ORD-exporting source is connected yet | A real ORD feed exists — then run both adapters (or a composite) on the background queue, each with its own cursor |
| Per-step species linking from free-text prose | Prose steps carry no `components` — guessing a SMILES from a name mid-sentence would fabricate structure; the coarse `StepKind` label and per-step temp/time are the deterministic floor | Wire the `eln-reaction-extraction` skill's per-field LLM to resolve named reagents per step (name→SMILES tool), still PR-gated |
| Durable multi-step "deep research" as a Temporal workflow | Research is interactive Q&A (MAF's job); `gather_evidence` + tools + the `deep-research` skill cover it conversationally without a durable job | A single research question needs many expensive fan-out steps (broad literature sweeps, batch computations) that must survive a restart |
| Compound notes so molecule/substructure hits cite a note directly | Molecules are indexed by SMILES, not yet as graph notes; `find_substructure_matches` returns SMILES and the agent bridges to reactions via `find_notes` | Compound notes exist (a later ELN step) — then a molecule retriever can cite `compound-<id>` and join gather_evidence directly |

> Source for the last six rows: `docs/research-review.md` (external 2025/2026 gap analysis).
> Two items were instead promoted to `BACKLOG.md` for a *now* decision — the evaluation/metrics
> layer and the chemical/bio safety layer — because they are cross-cutting, not deferrable modules.
