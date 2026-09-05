---
id: retrieval-coupling
metrics: [retrieval_recall, retrieval_precision]
output:
  query: coupling
reference:
  expected_note_ids:
    - playbook-pd-cross-coupling
    - reaction-suzuki-biaryl
    - reaction-amide-edc
    - reaction-zinc-negishi-biaryl
---
The broadest query in the set: **31** of the corpus's notes contain the literal "coupling", against
a `retrieval_top_k` of 8. So three quarters of the matches are cut before the agent sees them, and
what the cut keeps is entirely the ranker's decision — which is the property this case exists to
measure and the property a six-note corpus could not express at all.

The gold set is the four notes the term is *about* in this programme: the two coupling steps the
process runs (the Suzuki step and the amide step), the alternative step scouted beside them, and the
playbook that governs all of them. Everything else that matches — a compound that names its role in
a coupling, a campaign whose subject is the solvent, a qualification run — mentions it in passing,
which is exactly what a distractor should be. Precision is well below 1.0 here on purpose: a query
this broad legitimately returns more than the gold set.

Two of the notes the ranker keeps are worth naming, because they are what an honest distractor looks
like. `failure-mode-homocoupling-oxygen` scores second on term frequency and is not gold —
"coupling" is a *substring* of "homocoupling", so the note ranks on a word it does not contain.
`playbook-organozinc-preparation` is about preparing the reagent for a coupling rather than about
the coupling. Both are the right answer to a neighbouring question, which is the only kind of
distractor that teaches the gate anything.
