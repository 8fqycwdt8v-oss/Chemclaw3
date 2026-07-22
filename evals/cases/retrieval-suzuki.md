---
id: retrieval-suzuki
metrics: [retrieval_recall, retrieval_precision]
output:
  query: suzuki
reference:
  expected_note_ids: [reaction-suzuki-biaryl, campaign-suzuki-optimization]
---
The reaction and its optimization campaign both name "suzuki"; a query for it must surface both.
Recall and precision are 1.0 — the literal term is present in exactly the relevant notes.
