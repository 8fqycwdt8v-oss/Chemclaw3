---
id: retrieval-suzuki
metrics: [retrieval_recall, retrieval_precision]
output:
  query: suzuki
reference:
  expected_note_ids:
    - reaction-suzuki-biaryl
    - campaign-suzuki-optimization
    - optimization-campaign-suzuki-doe
---
The run, the campaign that optimized it and the DoE that preceded the campaign all name "suzuki"; a
query for it must surface all three. Ten notes in the corpus contain the term — the solvent swap,
the loading reduction, the Negishi comparison and the playbook each mention it in passing — so this
is a ranking case, not a filter case: the three notes the term is *about* have to beat the seven
that merely say it, inside a cut of `retrieval_top_k`.
