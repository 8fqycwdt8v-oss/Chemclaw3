---
artifact_refs:
- xtb.hess@GFN2-xTB+tblite+0.4.0:7f3a91c4d2b8:9e8d7c6b5a41#hessian.npy
calc_refs:
- xtb.opt@GFN2-xTB+tblite+0.4.0:7f3a91c4d2b8:0a1b2c3d4e5f
- xtb.hess@GFN2-xTB+tblite+0.4.0:7f3a91c4d2b8:9e8d7c6b5a41
compound_smiles: CC(=O)Oc1ccccc1C(=O)O
confidence: 0.7
created_by: human
id: job-aspirin-thermo
relations: []
source: seed-corpus
tags:
- computed
- thermochemistry
type: job-result
---

GFN2-xTB thermochemistry for [[computed-from:compound-acetylsalicylic-acid]] at 298.15 K, 1 atm.

- electronic energy: -47.913421 Hartree
- Gibbs correction: +71.84 kcal/mol
- lowest mode: 38.2 cm^-1 (a real minimum)

Computed from a converged optimisation; the Hessian behind it is stored, so the same geometry at
another temperature is arithmetic rather than a recomputation.