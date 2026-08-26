# `chemclaw.science.labels` — the reaction-label index

**Responsibility:** the derived, versioned, rebuildable view of every reaction corpus that the
faceted precedent questions are asked of — per-species roles, the named reaction, the conditions,
the structure features — plus the coverage statement every answer over it must carry.

This is not the record of truth. For an ELN reaction that is its `reaction_records` row and the entry
upstream it was transcribed from; for a patent corpus it is the source table. Both tables can be
dropped and refilled, and the only thing lost is the time it takes.

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

## Staleness is a query, not a flag

`labeller_version` NULL means never derived; a value below the current one means derived by a
superseded labeller. Both are found by one indexed scan, so "which entries are missing labels" is a
`WHERE` clause and nothing has to remember to mark anything. Same idea as `note_index.fingerprint`
(`infra/sql/035`) and `document_chunks.embedding_key` (`038`).

## What is deliberately absent

* **Fingerprint bits on `reaction_species`.** A 13M-reaction corpus is ~65M species rows over ~4M
  distinct structures; the bits live once per structure in `corpus_molecules` and join by `smiles`,
  which is already `standard_smiles`.
* **A widened `Role`.** See `vocabulary.py` — `Role` decides which side of the fingerprint
  boundary a species lands on and is writable from a tenant's YAML binding, so a sixth member is
  an arithmetic change, not a vocabulary change.
* **`ingest` imports.** `science/` may import `chemclaw.core` and nothing else. The recorded-role
  strings are named here as strings and pinned to `Role` by `tests/test_label_vocabulary.py`.
