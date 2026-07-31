---
id: retrieval-cross-coupling-literal-miss
metrics: [retrieval_recall, retrieval_precision]
output:
  query: cross-coupling
reference:
  expected_note_ids: [reaction-suzuki-biaryl, playbook-pd-cross-coupling]
---
The known-hard case (documents and measures the KM-4 literal-matching limitation). A Suzuki reaction
*is* a palladium cross-coupling, so a chemist searching "cross-coupling" should get both the reaction
and the playbook. But the reaction note never uses the literal string "cross-coupling", so the
substring retriever finds only the playbook: recall = 0.5, below the gate — the metric is *supposed*
to flag this. Precision stays 1.0 (the one note found is relevant). The mitigation is the agent's
query reformulation (the `deep-research`/`knowledge-graph-query` skills), which this lexical metric
does not exercise; if a future stemming/synonym layer lands, this case's recall should rise.
