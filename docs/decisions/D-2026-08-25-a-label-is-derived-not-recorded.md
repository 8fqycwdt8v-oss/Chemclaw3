# D-2026-08-25-a-label-is-derived-not-recorded — a reaction's roles and name are a derived index, not a wider `Role`

**Status:** accepted · **Date:** 2026-08-25 · Extends `D-120-a-data-source-becomes-a-manifest-the-second-config`
with a third thing a source may declare. Does not supersede anything.

## Context

A chemist wants to ask six questions this system could not answer at all:

1. Has this substrate been used in other reactions **as starting material**?
2. Give me conditions that worked for **similar products**.
3. Which **ligands** were used in the past for Buchwald couplings?
4. Find reactions matching this **reaction SMARTS**.
5. Which **workhorse conditions** were used for Buchwald couplings whose *product* carries this
   functional group?
6. How do we best **work up** reactions with this reagent?

A reaction, before this ADR, was three things: an `OrdReaction` value in flight, one
`reaction_fingerprints` row holding DRFP bits and nothing else, and a PR-gated Markdown note. That
is enough for "have we run a transformation like this", which is what DRFP answers, and it is
enough for none of the six. Every one of them is a *facet* question — it needs per-species roles, a
named reaction, conditions in columns, and a substructure index.

`skills/reaction-search/SKILL.md` already records what happens when a facet question is asked of a
whole-reaction fingerprint: a query built from the two core substrates scored **0.24** against a
real, same-class precedent whose recipe the corpus had encoded as reactants; the same query built
with the matching recipe scored **1.00**. The skill's advice — "try widening the query with
plausible reagents" — is a workaround for a missing index.

## Decision

**A second index, `reaction_labels` + `reaction_species` (`infra/sql/050`), written in two phases.**

### The record phase cannot be derived later, so it is written at ingest

`OrdReaction.transformation_smiles()` — the string `reaction_fingerprints` stores — deliberately
drops solvent and catalyst. It has to: leaving them in let a solvent swap dominate similarity, and
the number is in `ord.py`'s own docstring — two runs of one coupling in THF and 2-MeTHF scored
**0.82** against each other, less than two unrelated reactions sharing a solvent, and **1.00** once
the agents were excluded. Right for a fingerprint. Fatal for an index whose whole job is to answer
*which solvent, which ligand, which base*.

And there is no second chance to ask the source: `ElnAdapter` offers `fetch_new_entries(since)` and
nothing that reads one entry back by id. So `ingest_reaction` writes the record form
(`reaction_smiles()`, agents kept), the conditions, the workup text and one species row per
component carrying the role the source recorded — and `label_index` and `source` are **required
keyword arguments with no default**, because a default of `None` would let a caller quietly stop
writing the half of the row nothing can rebuild.

### Staleness is a query, not a flag

`labeller_version` NULL means never derived; a value below the current one means derived by a
superseded labeller. One indexed scan (`labeller_version IS DISTINCT FROM $1`) finds both. So "as
soon as entries are identified that miss these things" is a `WHERE` clause: a fresh corpus, a
re-recorded reaction and an upgraded labeller all produce work through the same predicate, and
nothing anywhere has to remember to mark anything.

`note_index.fingerprint` (035) and `document_chunks.embedding_key` (038) are the same idea applied
to two other kinds of derived data, and both were added after a flag failed to be set.

### `Role` is **not** widened

The obvious move — add `LIGAND`, `BASE`, `ADDITIVE` to `chemclaw.ingest.eln.ord.Role` — was
considered first and rejected on three counts, each load-bearing:

* **`Role` decides arithmetic.** `_AGENT_ROLES` chooses which side of `transformation_smiles()` a
  species lands on, so a sixth member changes every DRFP bit in `reaction_fingerprints`, forcing a
  `reaction_definition()` bump and a full re-index of the corpus.
* **`Role` is tenant-writable.** `warehouse/binding.py::ComponentBinding._role_vocabulary_is_real`
  validates each site's YAML `value_map` against it, so a widened enum lets a *data file* move a
  species across the fingerprint boundary with no code change and no review.
* **`ord.py` already argues the opposite.** It states that a base stays on the reactant side
  because it participates stoichiometrically and is part of what the transformation *is*. A `BASE`
  member would either contradict that or be a synonym of `REAGENT`.

So `SpeciesRole` is a second, *derived* vocabulary in `science/labels/vocabulary.py`, and the
recorded role is kept verbatim beside it. A row therefore says both what the source claimed and
what a model concluded, which is what lets `method` distinguish "Pistachio said Buchwald-Hartwig"
from "our SMIRKS matched Buchwald-Hartwig" — different evidence, and a chemist reading a frequency
table is entitled to know which.

### `provides` is never a skip

Each source declares a `labels:` block in its own `datasource.yaml` — the manifest, not `Settings`,
because `core/config/README.md`'s rule is *config says which and where; a manifest says what*.

The block has two fields and only one of them skips anything. `provides` names what the source is
*expected* to carry; a group listed there is **still derived for every row where the source left it
empty**. That is the whole of the requirement "the database will not have all these labels in the
beginning": Pistachio ships NameRxn names for roughly two thirds of its corpus, an ELN ships none,
and both are the same case. `provides` is read for exactly two things — the coverage report, and
the subset check on `override`, which is what re-derives a group the source *did* supply (an ELN's
roles are a typed column somebody chose, not a chemistry judgment).

There is deliberately **no `labels_enabled` setting**. `CHEMCLAW_DATA_SOURCES` plus a declared
block already answers "is anything being labelled here", and `durable/schedules.py` asks the
manifests — the third conditional Schedule in that file to do so, for the third time because a
second flag could only restate the source list or contradict it (D-018).

## Consequences

* Two new tables, both derived and both rebuildable: drop them, re-run the corpus drain and the
  label backfill, lose only time. Nothing reads a label as evidence without also reading its
  citation.
* `ingest_reaction` and `sync_entries` gained two required keyword arguments. Every call site in
  the tree and in the tests passes them; there is no default to forget.
* `reaction_species` carries **no fingerprint bits**. A 13M-reaction corpus is ~65M species rows
  over ~4M distinct structures; the bits belong once per structure, joined by `smiles` (already
  `standard_smiles`, so it joins by value with no surrogate key).
* A re-ingest whose `record_smiles` is unchanged **keeps** its derived phase — a note edit must not
  discard a backfill that took days — and one whose structures changed drops it and re-stales the
  row, because the name was about a different reaction. Both backends implement the same rule and
  `tests/test_label_index.py` drives both through it.
* `tests/test_label_vocabulary.py` pins the recorded-role map to `Role`, so a sixth member fails a
  test rather than silently landing every species of that role in `UNKNOWN`. That test exists
  because `science/` may import `chemclaw.core` and nothing else, so the map names the roles as
  strings — a deliberate, documented duplication with exactly one hazard, closed where it can be.
