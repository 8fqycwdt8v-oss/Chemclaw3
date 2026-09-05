---
id: retrieval-coupling-playbook-filter
metrics: [retrieval_recall, retrieval_precision]
output:
  query: coupling
  filters:
    type: playbook
reference:
  expected_note_ids: [playbook-pd-cross-coupling]
---
The same broad "coupling" query, narrowed by a `type: playbook` filter — exercises the retriever's
structured filter path. The filter takes the 31 matches down to the three playbooks that mention the
term, so the cut no longer engages and this case measures the filter rather than the ranking: recall
is 1.0 and precision reports how much of what survived the filter was gold.
