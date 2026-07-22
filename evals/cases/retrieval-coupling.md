---
id: retrieval-coupling
metrics: [retrieval_recall, retrieval_precision]
output:
  query: coupling
reference:
  expected_note_ids:
    - reaction-suzuki-biaryl
    - reaction-amide-edc
    - campaign-suzuki-optimization
    - playbook-pd-cross-coupling
---
A broad term that legitimately spans several notes — the recall case. All four coupling-related
notes contain the literal "coupling", so recall and precision are both 1.0.
