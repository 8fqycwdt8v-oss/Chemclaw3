---
id: solubility-benzene
metrics: [prediction_error]
output:
  predicted: -2.1269
  unit: log10(mol/L)
reference:
  actual: -1.64
---
Prediction-accuracy case: the ESOL/Delaney solubility predictor (`calc.solubility`)
vs. the held-out experimental aqueous solubility of benzene, log S = -1.64
(Delaney, J. Chem. Inf. Comput. Sci. 2004). Predicted -2.127 → absolute error 0.49,
within the 1.0 log-unit tolerance. Recompute `predicted` when the model version
changes; this file records the output under evaluation at its version.
