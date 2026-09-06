---
id: retrieval-reflux-conditions
metrics: [retrieval_recall, retrieval_precision]
output:
  query: reflux
reference:
  expected_note_ids:
    - reaction-suzuki-biaryl
    - reaction-fischer-esterification
    - reaction-heck-cinnamate
    - reaction-sonogashira-alkyne
---
Condition-term retrieval, and the case where the distractors are the most nearly relevant: eleven
notes contain "reflux", but only four of them are *runs performed at reflux*. The other seven are a
playbook arguing that reflux is not a setpoint, two solvents quoting their boiling points, a
solvent-swap campaign, a work-up failure and two operational rules. All of them are the right answer
to a slightly different question, which is what makes them worth ranking against.
