---
id: retrieval-organozinc-tag-filter
metrics: [retrieval_recall, retrieval_precision]
output:
  query: coupling
  filters:
    tag: organozinc
reference:
  expected_note_ids:
    - playbook-organozinc-preparation
    - reaction-zinc-negishi-biaryl
---
The `tag` filter, which had no gold case at all: `_eligible_notes` understands four filter keys and
only `type` was ever exercised here, so a change that broke tag narrowing moved no pinned number.
Nine notes carry `organozinc`; four of those also contain "coupling", and the two that describe the
coupling itself are the gold. A regression that ignores the tag would pull in the Suzuki step and
the cross-coupling playbook, which is a precision drop this case can see.
