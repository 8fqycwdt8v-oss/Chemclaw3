# `chemclaw.science.labels` — the reaction-label index

**Responsibility:** the derived, versioned, rebuildable view of every reaction corpus that the
faceted precedent questions are asked of — per-species roles, the named reaction, the conditions,
the structure features — plus the coverage statement every answer over it must carry.

This is not the record of truth. For an ELN reaction that is its `reaction_records` row and the entry
upstream it was transcribed from; for a patent corpus it is the source table. Both tables can be
dropped and refilled, and the only thing lost is the time it takes.

## The modules

| Module | What it is |
| --- | --- |
| `records.py` | the two rows of the index, and what each phase of writing them may set |
| `store.py` | the index itself — the derived, rebuildable view, and the staleness scan |
| `vocabulary.py` | `Role` and the recorded-role strings |
| `policy.py` | what one data source declares about its labels — the `labels:` manifest block |
| `facets.py` | one facet query, and the honest denominator every answer over it carries |
| `search.py` | the precedent questions, as thin shapings of one facet query |
| `molecules.py`, `reactions.py` | `corpus_molecules` and `corpus_reactions`: similarity and substructure over the corpus |
| `pattern.py` | an RDKit pattern fingerprint stored as set-bit indices, the screen substructure search uses |

## The two-phase row, and why

| Phase | Written by | Columns |
| --- | --- | --- |
| record | whoever ingested the reaction, from the canonical record | `record_smiles` (agents **kept**), `citation`, conditions, `workup_text`, one `reaction_species` row per species with its **recorded** role |
| derived | the background labeller, from `record_smiles` | `mapped_smiles`, `named_reaction`, `reaction_class`, `rxno_id`, `confidence`, `method`, and per species `derived_role`, `scaffold`, `functional_groups` |

The record phase cannot be derived later from what already exists, and that is the load-bearing
fact. `OrdReaction.transformation_smiles()` — the string `reaction_fingerprints` stores —
deliberately drops solvent and catalyst, because leaving them in let a solvent swap dominate DRFP
similarity (measured: 0.82 for one coupling in THF vs 2-MeTHF, 1.00 once excluded). Right for a
fingerprint, fatal for an index whose whole job is to answer *which solvent, which ligand, which
base*. And `ElnAdapter` offers `fetch_new_entries(since)` and nothing that reads one entry back by
id, so there is no second chance to ask the source.

`withheld` belongs to neither phase: it follows the ELN entry the row records. The ingest writes it
with every record phase, so a withdrawal sets it and a re-publication clears it; every read skips a
withheld row, because the app role cannot delete one.

**A row has a tier.** An entry whose source named a species without its structure
(`D-2026-09-27-a-reaction-without-a-structure-is-citable-not-searchable`) has a `citation-only` row of
the record phase alone: the species the source drew, each with the source's own role, plus the
conditions, yield and workup, and no `record_smiles`. The labeller never reads it and nothing is
derived for it (`stale()` and `store_labels()` skip the tier). The facet questions keyed on a drawn
species or a role count it in a tier of its own and mark it; a facet narrowing on a named reaction,
a product or a fingerprint neighbour cannot reach it
(`D-2026-10-23-a-facet-on-a-drawn-species-counts-the-citation-tier`). An entry changing tier
re-records its row and species from nothing, so a role refined for one record is never read as the
other's.

## Staleness is a query, not a flag

`labeller_version` NULL means never derived; a value below the current one means derived by a
superseded labeller. Both are found by one indexed scan, so "which entries are missing labels" is a
`WHERE` clause and nothing has to remember to mark anything. Same idea as `note_index.fingerprint`
(`infra/sql/035`) and `document_chunks.embedding_key` (`038`).

## What is deliberately absent

* **Fingerprint bits on `reaction_species`.** A 13M-reaction corpus is ~65M species rows over ~4M
  distinct structures; the bits live once per structure in `corpus_molecules` and join by `smiles`,
  which is already `standard_smiles`.
  A reaction's own bits live once per *reaction* in `corpus_reactions` (`reactions.py`), keyed
  `(source, id)` like `reaction_fingerprints`, so a hit joins back to `reaction_labels`.
* **A widened `Role`.** See `vocabulary.py` — `Role` decides which side of the fingerprint
  boundary a species lands on and is writable from a tenant's YAML binding, so a sixth member is
  an arithmetic change, not a vocabulary change.
* **`ingest` imports.** `science/` may import `chemclaw.core` and nothing else at module scope (its
  one declared lazy edge is `calc`'s best-effort hand-off to `publish`). The recorded-role
  strings are named here as strings and pinned to `Role` by `tests/test_label_vocabulary.py`.
