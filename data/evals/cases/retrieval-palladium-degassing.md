---
id: retrieval-palladium-degassing
metrics: [retrieval_recall, retrieval_precision]
output:
  query: palladium degassing
reference:
  expected_note_ids:
    - playbook-degassing-air-sensitive
    - playbook-pd-cross-coupling
    - campaign-catalyst-loading-reduction
    - failure-mode-homocoupling-oxygen
---
A two-term query, which is what exercises the rarity weighting: "palladium" is in thirteen notes and
"degassing" in five, so the rarer term should carry the ranking. Five notes contain both, and the
four gold ones are the notes that connect the two — why the catalyst needs the degas, what the degas
is, what happens without it, and the loading campaign that depends on it. The fifth is the Suzuki
run itself, which records that it was degassed without being about degassing.
