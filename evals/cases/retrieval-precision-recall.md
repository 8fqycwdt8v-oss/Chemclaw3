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
*live* retriever over a fixed gold corpus is the KM-13 `retrieval_recall`/`retrieval_precision`
path (`evals/retrieval.py` + `evals/cases/retrieval-*.md` over `evals/retrieval_corpus/`); this
case instead pins the generic set-based classification metrics on a static predicted/expected
pair, independent of any corpus.
