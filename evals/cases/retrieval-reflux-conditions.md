---
id: retrieval-reflux-conditions
metrics: [retrieval_recall, retrieval_precision]
output:
  query: reflux
reference:
  expected_note_ids: [reaction-suzuki-biaryl, reaction-fischer-esterification]
---
Condition-term retrieval: exactly the two reactions run at reflux mention it, so recall and precision
are 1.0. Guards that a body-text condition word still surfaces the right reactions.
