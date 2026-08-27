---
artifact_refs: []
calc_refs: []
compound_smiles: OB(O)c1ccccc1
confidence: 0.6
created_by: human
id: job-boronic-acid-pka
relations: []
source: seed-corpus
tags:
- computed
- pka
type: job-result
---

Predicted pKa for [[computed-from:compound-phenylboronic-acid]]: 8.8 (Lewis acidity of the boron,
as the boronate equilibrium).

Semiempirical prediction with an uncertainty of roughly ±1 unit — enough to say the boronate is
significant at pH 9 and not enough to quote a number in a report.

Seed content: no calculation row backs this figure, which is why `calc_refs` is empty here — a
real job-result cites the calculation keys its numbers came from, and `make kg-validate` checks
every cited key against the calculation store.