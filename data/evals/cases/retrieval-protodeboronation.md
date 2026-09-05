---
id: retrieval-protodeboronation
metrics: [retrieval_recall, retrieval_precision]
output:
  query: protodeboronation
reference:
  expected_note_ids:
    - failure-mode-protodeboronation
    - campaign-boronate-stability
---
The rare-term case: exactly three notes contain "protodeboronation", so the cut never engages and
recall here is a statement about the *filter*, not about the ranking. It is the control for every
other case in this set — a corpus where recall only ever moves because of the top-k cut cannot tell
a ranking regression from a matching regression, and this case is the half that moves when matching
breaks.
