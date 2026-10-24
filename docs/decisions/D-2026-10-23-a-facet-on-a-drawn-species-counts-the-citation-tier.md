# D-2026-10-23-a-facet-on-a-drawn-species-counts-the-citation-tier — A facet on a drawn species counts the citation tier

**Status:** accepted · **Date:** 2026-10-23 · **Decided by the owner 2026-10-24: option 2** ·
**Refines:**
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

**Decided by the owner on 2026-10-24: option 2.** It is the condition D-2026-09-27 named for
re-deciding step 4, it removes the inference that step 4 guarded against (nothing is derived from a
partial reaction), and it keeps the decided boundary — no fingerprint, no similarity — intact. Option
3 duplicates the facet logic over text. The build ships behind `make model-text-eval`
(D-2026-10-08-model-facing-text-changes-ship-behind-an-evaluation): the tool docstrings, result
fields, verdicts and the `reaction-search` skill are model-facing text.

What is built:

1. **A row of the record phase only.** `infra/sql/136_reaction_label_tier.sql` adds
   `reaction_labels.tier` (`structured` | `citation-only`), makes `record_smiles` nullable and ties
   the two with a `CHECK`. `record_phase` writes the row for a citation-only record: the species the
   source drew (a species it only named has no row), each with the source's own role mapped to the
   vocabulary and never refined, plus conditions, yield and workup. The labeller's scan and
   `store_labels` skip the tier, so nothing is derived for it; no fingerprint or molecule row is
   written. An entry changing tier re-records its row and species from nothing, and `withheld`
   narrows to a withdrawn entry's row and the structured row an entry left behind.
2. **A facet counts the tier only when it can answer.** `Facet.reaches_citation_only` is false when
   the facet narrows on a named reaction, an RXNO id, a product structure or group, or a fingerprint
   neighbour; those stay structured-only, and their verdicts keep the `NOT SEARCHED` pointer.
   `substrate_precedent`, `workup_precedent` and `reagent_frequency` without those narrowings count
   both tiers. The read is one statement per tier, so the structured read keeps its ordering index.
3. **Every figure states its tier.** A hit carries `tier`; `AgentCount` splits `structured_count`
   from `citation_only_count` and gives each tier its own median yield; `FrequencyReport` and
   `CorpusCoverage` split the reactions counted; the `COMPLETE:` verdict says it counts structured
   reactions and, beside them, the citation-only records, and a `CITATION-ONLY TIER:` sentence says
   nothing is derived for them, that a ligand, base or additive filter does not match them (their
   roles are the source's own word) and that a species the source only named is never matched.
4. **A record with no row yet is said so.** An entry stored before 136 has the record and not the
   row until its source is re-synced from the epoch; the sync writes it once (a replayed citation-only
   entry is skipped only when its row exists), and until then a verdict says
   `NOT COUNTED: N citation-only ELN record(s) on file have no label row yet`.
5. **Smaller consequences.** `substrate_precedent` and `workup_precedent` standardize the query (a
   Kekule spelling now finds what the standardized one does); with nothing labelled the tools ask
   the index for a version no row carries, so the citation-only tier is still read and a structured
   facet still reports "not answerable yet" with the real corpus size.

## Consequences

`tests/test_ord_citation_tier.py`'s "never counted" assertions are replaced by "counted and marked"
ones, and `tests/test_citation_tier_labels.py` holds the rest: the tier is never offered to the
labeller, a stale fingerprint cannot hand a facet its key, a tally splits by tier, a tier change
resets the row, the replay writes a missing row once, two tenants keep their own rows, and
migration 136's block derives the flag and clears what a rolled-back image derived. The rollback
costs a silent row in the runbook's list. The behaviour is live-evaluated before it ships: the plan
is in the pull request.

Revisit when: the live model-text evaluation of this batch is run (`make model-text-eval`), and
reads worse than the shipped text (this option then reverts to option 1), or a facet is proposed
that would match a named-only species by its text.
