# D-2026-08-03-a-metric-must-declare-what-it-can-see — a metric must declare what it can see

**Status:** accepted · **Date:** 2026-08-03

## Context

A live run of 36 corpus-grounded probes graded **19 of 36 answers as fabrication**. Nine of those
verdicts were checked against what the tools had actually returned, and **all nine were false**
(`docs/archive/live-grounded-2026-08-03.md`):

| the grader said | the tool had returned |
|---|---|
| "invents PDEs 100/10/1 Pd, 3000/300/30 Cu" | exactly those six values |
| "the property table is entirely fabricated" | HOMO −11.827, dipole 4.558, S charge +1.395 — the answer's −11.83, 4.56, +1.40 |
| "fabricates copper/lead/silver plumbing controls" | the hazard rule's own `explanation`, Bretherick's citation included |
| "invents Benigni & Bossa 2011" | the genotoxicity table's `citation` field, six occurrences |
| "four ids **mechanically verified as absent from the corpus**" | all four on disk, all four returned by one retrieval call |

One number caused it. `ToolResultEvent.preview` is capped at 200 characters — correctly, for a
browser — and `evals/live._score_citations` scanned those previews while `gather_evidence` returns
up to 40 chunks. Every id and every value past the first chunk read as invented.

The second half is what makes it a decision rather than a bug. The judge prompt already warned that
"previews are truncated, so absence here is NOT proof a number was invented" — and then handed over
the derived list under *"trust this over your own reading"*. The derived list came from the same
truncated previews. So the prompt cautioned about the evidence and then vouched for a conclusion
drawn from it, and the grader resolved the contradiction the way it was told to: by trusting the
number, and escalating "not in the preview" into "mechanically verified as absent from the corpus"
— a claim the harness never made and cannot make.

## Decision

**A signal a consumer is told to trust must state what it can see, in the same place it is
presented.** Concretely, in this system:

1. **A field serving a human and a field serving a scorer are different fields.** `ToolResultEvent`
   now carries `preview` (truncated, for a UI) *and* `note_ids` (untruncated, for a check). Widening
   the preview would have fixed the check by destroying the budget the preview exists to keep; the
   two questions get two answers off one event.
2. **The caveat travels with the signal.** The heading that presents `uncited_note_ids` to the judge
   now says what it asserts — this id was not in front of the model this turn — and what it does
   not — anything about whether the note exists. A caveat placed two paragraphs away from the
   number it qualifies is a caveat that will not be applied.

## Consequences

- Every fabrication figure produced by this harness before today is void. The archived run is kept
  with its numbers *and* their refutation, because the refutation is the more useful record.
- `_score_citations` became a set difference, which incidentally closed a hole the substring scan
  had: a returned `playbook-degassing-old` used to ground a cited `playbook-degassing`, and both
  are in the committed corpus.
- `mentioned_ids` (`kg/note.py`) scans for this system's *own* note serializations rather than
  guessing at id shapes in arbitrary text, and `tests/test_note.py` pins those fixtures to real tool
  output — so a fourth serialization breaks a test instead of silently narrowing the scan. Writing
  that test is what surfaced that the envelope's quotes arrive JSON-escaped (`id=\"X\"`), which an
  idealized fixture would have hidden until the next live run.

## Why this keeps happening

This is the third instance of one shape in two weeks, and naming the shape is the point of writing
it down: **one data structure, two consumers, and nobody checking what each actually reads.**

- `_verifier_prompt` rendered every `EvidenceChunk` while `verify_claims` read only the id set —
  a 40× prompt blow-up (`D-2026-08-02-grounding-is-what-this-turn-saw`, fixed in the review after).
- `ScreenResult.verdict` was a bare `property`, so `model_dump()` dropped the "this is not a safety
  assessment" disclaimer and it reached zero production callers.
- `_score_citations` read the UI's preview to answer a grounding question.

In all three the code was locally reasonable and the defect lived in the gap between two readers.
The rule that would have caught all three: **when a metric and the thing it measures disagree,
establish what the metric can see before believing either.** That is `CLAUDE.md`'s "measure it,
don't argue it" pointed at the measuring instrument itself.
