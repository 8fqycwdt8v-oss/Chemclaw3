---
name: development-report
description: >-
  Judgment for drafting a cited development report from the system's own data: decompose the
  request into sections, retrieve evidence per section, discard any claim not backed by a
  retrieved source note, and keep evidenced history separate from transferred analogy.
---

# Development report

Holds the *judgment* for the report harness. The capability is deterministic: `report.harness`
fans each section's query out to the retrievers (graph, fingerprint search), collects cited
`EvidenceChunk`s, marks a section with no evidence as unsupported, and `verify_claims` drops any
synthesized claim whose citations were not actually retrieved. This skill decides how to
decompose the request, what to write from the evidence, and — critically — what *not* to write.

## Decompose into answerable sections

- Turn the report request into concrete sections, each with a **narrow query** and an explicit
  **memory layer** (`evidence`/`episodic`/`semantic`). A section is a question the retrievers can
  actually answer, not a theme. Vague sections retrieve noise.
- Choose the layer honestly: a "what did we observe" section is `evidence`/`episodic`; a "what
  generally works" section is `semantic` (playbooks). The report declares this per section so the
  provenance layers stay structurally separate — do not mix them in one section.

## Write only what the evidence supports

- Every sentence must trace to a retrieved source note. Run each drafted claim through
  `verify_claims`: if it does not cite evidence that was actually returned, it is **discarded**,
  not reworded. This is the hard line against invented statistics — a "yields rose 40% over three
  years" with no note behind it does not go in the report.
- An unsupported section stays marked unsupported. "We found no data on X" is a valid, useful
  report finding; a fabricated paragraph is not. Never fill a gap to make the report look complete.

## Keep evidenced and analogical apart

- Present episodic/evidence findings (what was actually done, cited) separately from semantic
  playbook guidance (a generalization transferred by analogy). Label which is which so the reader
  weighs them correctly.
- Where a transferred expectation conflicts with a cited experiment, the experiment wins and the
  conflict is surfaced.

## The draft is a proposal

- The report is agent-authored and enters through the PR-gate (5b.7, D-005): a process chemist
  validates it before it is relied upon. Write so every claim can be checked against its linked
  source in one pass — that traceability is the whole point.
