---
id: retrieval-zinc-negishi-last-id
metrics: [retrieval_recall, retrieval_precision]
output:
  query: zinc
reference:
  expected_note_ids:
    - reaction-zinc-negishi-biaryl
    - playbook-organozinc-preparation
---
**The case whose gold note sorts last.** `reaction-zinc-negishi-biaryl` is the alphabetically final
id in the corpus, and thirteen notes contain "zinc" (the term is a substring of `organozinc`, so the
whole reagent-preparation cluster competes). Against a cut of 8, any ranker that falls through to
note id drops this note first — which is precisely what the pre-BM25 `(-coverage, -confidence, id)`
ordering did, and what nothing in the six-note corpus could ever have shown.

The two gold notes are the ones that answer "how is the zinc route run": the performed Negishi
coupling and the playbook for preparing the organozinc. The four zinc salts and metals, the
activation screen, the insertion failure, the titration rule, the equivalents study, the Negishi
scouting campaign and the xTB aggregation result are all genuinely about zinc, all sort earlier, and
are all the wrong answer to this query.
