# D-2026-08-01-a-rule-that-counts-cannot-be-a-chain — A rule that counts cannot be a chain

**Status:** accepted · **Date:** 2026-08-01 · **Extends:** D-080 (the cited hazard rule table, one
reference molecule per rule) · **Implements:** the full-codebase review's two safety findings

## Context

`science/safety/screen.py` exists to give a chemist the obvious structural alerts before they run an
agent-proposed procedure. Its module docstring is explicit that "no rule matched" must never read as
"safe", because an over-trusted screen converts an absence of knowledge into apparent assurance.

Two defects made it return a false clean on the exact molecules it exists for.

**The polynitro rule matched only ortho.** Its SMARTS hung the second nitro group off the ring atom
closed by the `1` bond of a written six-atom chain, so it matched 1,2-dinitrobenzene and nothing
else. Measured on shipped code:

```
TNT            -> []
picric acid    -> []
1,3-DNB        -> []
1,4-DNB        -> []
1,2-DNB        -> [('polynitro-aromatic', 'high')]
```

No other rule caught them. This is live on both `screen_hazards` (MCP) and the `kg-validate` hazard
gate, which therefore demanded no `## Hazards` section for an agent-authored nitration procedure.

It survived because `tests/test_safety.py` *and* `data/evals/cases/hazard-rule-recall.md` both used
1,2-dinitrobenzene as the rule's reference molecule. D-080's "one reference molecule per rule"
discipline is blind here **by construction**: one molecule can demonstrate a motif, and cannot
demonstrate a count or a position.

**The hazard gate could not see a `bo-candidate`'s structures.** `structures_in` harvests SMILES from
`note.compound_smiles` and from backticked code spans; `note_from_campaign_result` wrote
`- molecule: CCN=[N+]=[N-]` as plain prose and set no `compound_smiles`. So `hazard_problems(note)`
returned `[]` for every machine-minted candidate — the one note type proposing an experiment nobody
has run, and the type the gate's own docstring names as the reason it exists. The fixture in
`tests/test_safety.py` backticked values the real writer does not emit, which is why a test suite
that appeared to cover this did not.

## Decision

**A rule whose words name a count gets a count, not a longer chain.** `_StructuralRule` gains
`min_matches: int = 1`, and matching becomes
`len(GetSubstructMatches(pattern)) >= rule.min_matches`. `polynitro-aromatic` becomes a nitro group
on an aromatic carbon with `min_matches: 2` — which is what "polynitro" literally means.

This is an abstraction with one caller, which CLAUDE.md otherwise forbids. It is accepted here for a
stated reason recorded at the field: a count is not expressible as a substructure boolean, so the
alternative is not a simpler mechanism but three rules (ortho, meta, para) sharing one id and one
explanation.

**`maxMatches` is deliberately not used** as a short-circuit. It caps the *raw* embeddings collected
before uniquification, so a symmetric pattern could be truncated below its true unique count — a
silent false negative in a safety screen, bought for a micro-optimisation.

**The biphenyl false positive is accepted and documented.** 4,4'-dinitrobiphenyl now flags, though
its nitro groups are on different rings. In a hazard screen a conservative false positive is the
correct direction of error, and the rule's own comment says so rather than hiding it.

**A note must carry its structures; the extractor does not guess.** `note_from_campaign_result` sets
`compound_smiles` when the recommendation names exactly one molecule — following
`ingest/eln/note.py::_principal_product`'s reasoning that a wrong `compound_smiles` is worse than
none, because it is what a by-compound search returns — and emits parameter values so their
structures are extractable. Both declared ways a campaign names a molecule are resolved: a
featurized categorical's `structures` map, and a library campaign's levels, which *are* SMILES.

**A fixture for a machine-written note is built from the writer.** A fixture that hand-writes what a
producer is supposed to produce cannot catch a producer that does not produce it.

## Consequences

- `rules.yaml`'s header now states that one reference molecule is insufficient for any rule whose
  words say "multiple", "poly", "adjacent" or "on one ring" — those are counts and positions, not
  motifs.
- A new eval case pins substitution-pattern recall separately, because the existing
  `hazard-rule-recall` case still scored 1.0 with the bug present.
- **Every other rule in the table was audited** — roughly 90 molecules, one that should match and one
  near-miss per rule. `polynitro-aromatic` was the only wrong rule. Four are narrow rather than
  wrong (`peroxide` and `n-halamine` miss the sanitised ionic spellings; `hydrazine` excludes fully
  substituted free hydrazines; `complex-hydride-with-chlorinated-solvent` covers gem-polyhalides,
  which is what the documented incidents are). Those are recorded, not widened — widening a hazard
  rule on taste is how a table stops being cited.
- The searched-space listing deliberately does **not** get SMILES dumped into it: that would screen a
  500-molecule library instead of the proposed experiment, against D-157's bounded listing.
