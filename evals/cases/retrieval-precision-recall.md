---
id: retrieval-precision-recall
metrics: [precision, recall, f1]
output:
  predicted_note_ids: [reaction-101, reaction-102, reaction-999]
reference:
  expected_note_ids: [reaction-101, reaction-102, reaction-103]
---
Classification-metric case (plan F10-F1): a retriever returned three note ids, two of
which are relevant (`reaction-101`, `reaction-102`) and one spurious (`reaction-999`),
while a third relevant note (`reaction-103`) was missed. So precision = 2/3 ≈ 0.667,
recall = 2/3 ≈ 0.667, and F1 = 0.667.

This is a *pinned* predicted-vs-expected case: it keeps the `precision`/`recall`/`f1`
metrics under the versioned case-set (so a change that breaks them fails `make eval`) and
gives the drift check (F10-F2) a retrieval-quality number to watch over time. Scoring a
*live* retriever's output against a query is `evals.retrieval.run_retrieval_eval`, which
runs the retriever and produces exactly this predicted-vs-expected shape; the expected ids
there come from the deployment's own knowledge graph, so those cases are deployment-local,
not committed here.
