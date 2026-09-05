---
id: retrieval-boronic-acid
metrics: [retrieval_recall, retrieval_precision]
output:
  query: boronic acid
reference:
  expected_note_ids:
    - compound-4-methoxyphenylboronic-acid
    - playbook-boronic-acid-handling
    - campaign-boronate-stability
    - failure-mode-protodeboronation
---
Two common terms rather than one rare one: fifteen notes contain "acid" and nine contain both terms,
so the complete-match rule does the first cut and the ranking does the rest just inside `top_k`. The
gold four are the reagent, the rule for handling it, the campaign that measured its stability and
the way it is lost. The five other complete matches — the Suzuki and Negishi runs, the oxygen
failure, the base interaction and the conformer job — each use the boronic acid without being about
it.
