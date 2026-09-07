# `science/bo` — the Bayesian-optimization engine

Pure computation. BoFire is the engine, kept behind this package's own neutral types so agents,
skills and workflows never import BoFire directly (D-012, gate G6), and `engine.py` is the only
module that touches it.

| file | what it is |
|---|---|
| `problem.py` | the neutral vocabulary: parameters, objectives, observations, and every precondition a campaign has to survive |
| `engine.py` | the BoFire boundary — seed a design, propose candidates, interrogate the fitted surrogate |
| `objectives.py` | name → objective, because a Temporal workflow cannot carry a callable across its boundary |
| `featurize.py` | descriptors for a categorical, so the surrogate can tell two ligands apart |
| `campaign_record.py`, `campaign_record_store.py` | what a campaign *is* — identified by its decision space — and where that record lives |
| `progress.py` | whether an optimization is still finding anything, judged against the assay's own noise |
| `benchmarks/` | a real HTE dataset wrapped as a problem plus an objective |

**The ask/tell loop is not here.** It is the durable `BoCampaignWorkflow` in `connectors/bo`, which
is the only form a campaign ships in; the in-process loop that used to sit beside `engine.py` had no
caller in this package for months and now lives in `tests/bo_harness.py`
(`D-2026-09-07-a-driver-with-no-caller-is-not-a-capability`).
