# D-108 — One conformer ensemble, one reaction composite: the duplicates are removed

D-107 kept two implementations of two capabilities through a merge and recorded that a
*decision* was owed rather than pretending the merge had made one. This is the decision,
and it was taken on the user's instruction: remove the older tools and replace them with
the framework this branch built.

**What was removed.** `calc/conformer_ensemble.py`, `workflows/conformer_job.py`,
`workflows/conformer_models.py`, `workflows/conformer_activities.py`,
`agents/conformer_tools.py` (tools `submit_conformer_ensemble_job`,
`get_conformer_job_status`), `calc/reaction_energy.py` and the `estimate_reaction_energy`
tool — with their four test modules and four config settings.

**What replaced them.** `calc/conformers.py` behind `sample_conformers`, and
`calc/reaction.py` behind `compute_reaction_energy`. Both route through the one durable xTB
job (`XtbJobSpec` discriminated union → `XtbJobWorkflow`) and are polled with the one
`get_job_status`, rather than each capability carrying its own workflow, its own models, its
own activities and its own status tool.

### Why the replacements are strictly better, and where they are not

The conformer ensembles are not close. ETKDG + MMFF prune + GFN2 singles enumerates
*embeddings*; CREST searches conformational space by metadynamics, and it returns two things
the older path structurally could not: **rotamer degeneracies**, without which n-butane's
anti population comes out at 73% against a measured 59%, and the **conformational entropy**
that every single-conformer free energy is missing. It also feeds `level="thorough"` in the
reaction composite, which the standalone workflow could not.

The reaction pair is closer, and the honest reading is that they answered *overlapping*
questions rather than one. The removed screen differenced cached single points on
force-field geometries; the composite optimizes every species, can add Hessians for ΔH/ΔG,
and refuses an unbalanced equation instead of returning a difference that includes whatever
atoms the two sides do not share. The screen's one genuine capability — the thermal-hazard
flag — was **moved onto the composite** (`is_strongly_exothermic` against the same
configured threshold) rather than dropped, and is pinned by its own test. Consolidating is
not the same as losing a feature, and the difference is exactly that port.

**Two costs, stated rather than buried.**

- The removed screen ran on **cached single points and no optimization**, so it was seconds
  where the composite is minutes. `level="quick"` is the equivalent gear — it optimizes but
  skips every Hessian — and the exotherm flag is available there. It is still slower, and
  that is a real trade for correctness (a screen on an unrelaxed geometry is differencing
  two arbitrary conformers).
- CREST is an **optional binary**; the ETKDG path needed only RDKit. The deployment image
  installs both `xtb` and `crest` (`deploy/Containerfile`), so this costs nothing where the
  system actually runs — but a bare `pip install` dev environment now has no conformer
  ensemble at all, where before it had a weaker one. `crest_cli.run` already names the
  missing binary and says which capabilities it takes with it.

### The queue was wrong too, which the consolidation fixed for free

The standalone conformer workflow sat on the **`background`** queue — many light workers.
A CREST search is minutes of saturated CPU, which is the definition of the **`hpc`** queue
(D-006). Folding it into the xTB job put it on the right one, and it is now one queue choice
for every expensive xTB task rather than a decision repeated per capability. Pinned by a test
in `tests/test_workers.py`, which previously asserted the wrong queue and now asserts why.
