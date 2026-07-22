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
structured filter path. Only the playbook survives, so recall and precision are 1.0 against the
single expected source.
