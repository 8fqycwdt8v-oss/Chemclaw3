# D-2026-10-23-a-facet-on-a-drawn-species-counts-the-citation-tier — A facet on a drawn species counts the citation tier

**Status:** proposed · **Date:** 2026-10-23 · **Refines:**
`D-2026-09-27-a-reaction-without-a-structure-is-citable-not-searchable` (its step 4 and its
`Revisit when:`) · **Answers:** the owner call in the `BACKLOG.md` row *"A citation-only record is
pointed to by a facet answer, never counted in it"*

## Context

D-2026-09-27 stores a record whose source names a species without its structure as
`citation-only`: citable, and in no fingerprint, molecule or label row. Its step 4 excluded the
label row for a stated reason — the labeller derives an atom mapping and a named reaction from
`record_smiles`, which a partial reaction would turn into an inference — and its `Revisit when:`
named the condition for re-deciding it: a label row with named-only species and no derivation.

What the facet tools do at `HEAD`, pinned by `tests/test_ord_citation_tier.py`
(`test_a_structural_negative_names_the_citation_only_record_that_draws_the_query`,
`test_a_complete_coverage_says_it_counts_structured_records_only`; both pass on this branch): every
facet answer (`substrate_precedent`, `conditions_for_similar_*`, `reagent_frequency`,
`workup_precedent`, `reactions_making_substructure`) carries `unsearched` — the count of
citation-only records and, by a text check over their bodies, up to five ids of those that list the
queried structure as drawn — and its verdict tells the model to `expand_note` them. They are
**pointed to, never counted**: a base or ligand tally, a yield ranking, a "COMPLETE: all N" is over
structured records only. On the mock's seeding that tier is the Perera flow-Suzuki screen, 5,760 of
10,011 records (D-2026-09-27's measurement), every one with a drawn electrophile, base, ligand,
catalyst and yield and a coupling partner given only as the paper's shorthand.

The open question is whether a facet keyed on a species the source **did** draw is the "structure
search" D-2026-09-27 excludes. It is an identity match on a stated structure, not a similarity
ranking, and it derives nothing from the missing species.

## Options

1. **Yes, it is structure search: keep pointing, never counting** (status quo). No work. A facet
   answer about a drawn electrophile, base or ligand ignores the larger half of the seeded HTE data
   and leaves the tally to the model, five `expand_note` calls at a time.
2. **No: a label row with no derivation, counted and marked.** `ingest/labels/record.py::record_phase`
   emits a row for a citation-only record carrying its drawn species, conditions and yield, with no
   `record_smiles` and a tier flag; the enricher never derives a role, mapping or named reaction for
   it. Facets on a drawn species count it, and the verdict splits the count ("N structured, M
   citation-only; a named-only species is never matched"). Similarity and fingerprint search stay
   closed. Costs: a migration (`reaction_labels.record_smiles` nullable, a tier column), the
   enricher's skip, each facet query's tier split, the verdict and skill text, and a live eval
   showing answers improve and the model does not read a named-only partner as unknown-but-absent.
3. **Tally from record bodies at answer time.** Extend `ReactionRecordStore.citation_only` to parse
   conditions out of the rendered bodies and add them beside the indexed tally. No migration, but a
   second facet engine over Markdown the record renderer is free to change.

## Decision

Proposed: **option 2**, built behind the live eval. It is the condition D-2026-09-27 named for
re-deciding step 4, it removes the inference that step 4 guarded against (nothing is derived from a
partial reaction), and it keeps the decided boundary — no fingerprint, no similarity — intact.
Option 3 duplicates the facet logic over text. Option 1 remains the safe default if the eval does
not show a better answer.

## Consequences

If accepted: one migration, `record_phase` and the enricher change, the facet queries and verdicts
gain a tier split, and `tests/test_ord_citation_tier.py`'s "never counted" assertions are replaced
by "counted and marked" ones. The live-eval half cannot run on this machine (no model key); it is
the gate for shipping the behaviour.

Revisit when: the live eval comparing facet answers with and without the citation tier is run
(`src/chemclaw/evals/`), or a facet is proposed that would match a named-only species by its text.
