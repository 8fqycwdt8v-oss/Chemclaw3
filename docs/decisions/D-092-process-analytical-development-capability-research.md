# D-092 — Process/analytical-development capability research: quick wins, one durable big win, and what was rejected

A deep survey of open-source ML/cheminformatics and fast-ab-initio packages for chemical and
analytical process development (data-source connectors like LIMS explicitly out of scope), asking
specifically what could be added through the **existing** connector seams — a fast calculator
(`calc/` + the calculation store), an MCP capability server, or a Temporal workflow — with no new
ad hoc wiring. Landed as five additions, all through those exact seams, plus two candidates
researched and deliberately **not** built.

**Quick wins (fast, cached calculators/tools, zero new dependencies):**

- `predict_developability_profile` (`calc/descriptors.py`) — an RDKit-only physicochemical panel
  (MW, LogP, TPSA, H-bond counts, rotatable bonds, Fsp3, QED) plus Lipinski/Veber flags. Every
  descriptor is already computed by RDKit (already a dependency); the only gap was that nothing
  exposed the panel itself, versus the four descriptors buried inside the ESOL solubility model.
- `predict_logd` (`calc/logd.py`) — pH-dependent lipophilicity, composing the existing cached
  `predict_pka` with Crippen LogP via Henderson-Hasselbalch. No new cache entry (the expensive
  half, xTB pKa, is already memoized); inherits `calc.pka`'s neutral-O-H/S-H-acid domain limit.
- `estimate_reaction_energy` (`calc/reaction_energy.py`) — a reaction electronic-energy /
  exotherm screen from cached per-species GFN2-xTB single points, weighted by stoichiometry.
  Advisory, like the structural hazard screen (D-080) — a flag, never a safety certification.
- `generate_screening_design` (`bo/engine.py::factorial_design` + `bo/problem.py::ScreeningDesign`)
  — a full-factorial **categorical** screening design (e.g. every catalyst x solvent x base
  combination), via BoFire's `FractionalFactorialStrategy` on an all-categorical domain (the
  non-deprecated replacement for the now-deprecated `FactorialStrategy`). Distinct from
  `suggest_next_experiment`'s adaptive one-batch-at-a-time proposals. Rejects a continuous
  parameter outright (gate G4) rather than silently dropping/fractionating it.

**Big win (durable Temporal workflow, zero new dependencies):** `ConformerEnsembleWorkflow`
(`workflows/conformer_job.py` + `conformer_activities.py` + `conformer_models.py`, pure algorithm
in `calc/conformer_ensemble.py`) — an RDKit ETKDG conformer ensemble, MMFF-pruned, then
Boltzmann-weighted over per-conformer GFN2-xTB energies. `calc.xtb` approximates each molecule as
one rigid seeded geometry; a flexible molecule's solution-phase behavior is more honestly read
from a population of conformers. An ensemble (tens of xTB single points) is materially heavier
than the inline fast-calculator's sub-second budget but is pure local CPU work, not a remote HPC
submission — so it follows `BoCampaignWorkflow`'s shape (local activities on the light
`background-jobs` queue), not `QMJobWorkflow`'s submit/poll shape. `calc/xtb_engine.py` gained one
shared primitive (`positions_bohr`, factored out of `geometry`) so the ensemble reads a specific
already-embedded conformer instead of re-embedding one each time — a DRY refactor, not new science.
Agent tools `submit_conformer_ensemble_job`/`get_conformer_job_status` mirror `agents.qm_tools`
exactly (D-002's thin-adapter shape).

**Researched and deliberately not built**, both for the same reason:

- **ML interatomic potentials as a fast-ab-initio surrogate** (ANI-2x/TorchANI, MACE-OFF/MACE-MP).
  `torchani` was installed and inspected directly in this environment: current releases pull in
  `huggingface-hub`/`hf-xet`, and `torchani.models.ANI2x()` fetches its pretrained weights from the
  Hugging Face Hub on first use rather than shipping them in the wheel (this changed from older
  releases that did bundle weights). That is a runtime external-data dependency, which is exactly
  what D-089 says this system does not have — `tests/test_no_egress.py` enforces the source-literal
  form of that rule, but the principle is broader than what a host-literal grep can catch. Revisit
  only if a deployment vendors the weight files into the container image at build time as an
  explicit, reviewed infrastructure decision (D-089's own escalation path) — not as a quiet runtime
  fetch.
- **Retrosynthesis (AiZynthFinder)**. The `DEFERRED.md` trigger — "after the spine + graph +
  fingerprint layers exist" — is now met (ECFP4/DRFP fingerprint search shipped in F11). It still
  is not built: AiZynthFinder's pretrained USPTO models and stock file are fetched via a
  `download_public_data` step from a public host, the same runtime/deploy-time external-fetch
  problem as the ML potentials above, for the same reason not solved here. `DEFERRED.md` updated
  to record the sharpened blocker (not "no fingerprint layer yet", but "no vendoring story").

`bo/engine.py`'s docstring is updated (still "the only module that touches BoFire") to note
`factorial_design` as a second BoFire-touching adapter alongside the BO strategies, not a
boundary violation — it lives in the same file specifically to keep that claim true.
