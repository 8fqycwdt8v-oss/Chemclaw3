---
id: retrieval-cross-coupling-literal-miss
# This case exists to demonstrate the gate firing, so its failure is the expected
# result rather than a regression — see `EvalReport.regressions`.
expect_pass: false
metrics: [retrieval_recall, retrieval_precision]
output:
  query: cross-coupling
reference:
  expected_note_ids: [reaction-suzuki-biaryl, playbook-pd-cross-coupling]
---
The known-hard case (documents and measures the KM-4 literal-matching limitation). A Suzuki reaction
*is* a palladium cross-coupling, so a chemist searching "cross-coupling" should get both the
reaction and the playbook. But the reaction note never uses the literal string "cross-coupling", so
the substring retriever finds only the playbook: recall = 0.5, below the gate — the metric is
*supposed* to flag this. The mitigation is the agent's query reformulation (the
`deep-research`/`knowledge-graph-query` skills), which this lexical metric does not exercise; if a
future stemming/synonym layer lands, this case's recall should rise.

On the grown corpus the case also shows the *widening* rule doing its job: 31 notes match the term
"coupling" and 3 match both terms, so the complete-match set is what is returned and the 28
single-term notes never dilute it. Precision is 1/3 rather than the 1.0 the six-note fixture gave:
the two other complete matches are campaigns that name a cross-coupling in passing, and that they
are returned at all is the honest cost of requiring only that every term appear somewhere.
