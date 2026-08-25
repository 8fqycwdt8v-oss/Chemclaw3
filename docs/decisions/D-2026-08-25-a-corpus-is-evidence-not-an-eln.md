# D-2026-08-25-a-corpus-is-evidence-not-an-eln — a bulk reaction corpus has no ingest half and no PR-gate

**Status:** accepted · **Date:** 2026-08-25 · Applies
`D-2026-08-06-a-share-is-mounted-not-called`'s line to reactions. Lands beside
`D-2026-08-25-a-lakehouse-arrives-on-two-seams-not-one`, which attached the same corpus on its
*vector* seam the same day; see "Two seams, one table" below. Builds on
`D-2026-08-25-a-label-is-derived-not-recorded`, which defines the index it fills, and on
`D-2026-08-04-the-schema-is-a-file`, whose binding engine it reuses.

## Context

The Pistachio reaction corpus (NextMove) is arriving as a table in the site's warehouse: millions
of reactions extracted from patents, most carrying a NameRxn classification and an atom mapping,
and a substantial fraction carrying neither.

The obvious move is to attach it the way every reaction source is attached today: an `ingest:` half
implementing `ElnAdapter`, drained by `ElnSyncWorkflow` under a `sync_cursors` watermark, ending at
the PR-gate. It is the wrong move, and not because of size.

## Decision

**A reaction corpus declares `retrieve:` and no `ingest:` half, and is drained into the label
index's record phase by its own workflow.**

### Five paths, each of which breaks

Every one is real code in this tree, not a hypothetical:

* `durable/memory_jobs.py::read_corpus` calls `fetch_new_entries(datetime.min)` on **every** active
  ingest half and materialises every `OrdReaction` into the worker's heap. Three memory workflows
  do this per cycle.
* `memory/similarity.cluster_by_similarity` is then O(n²) pairwise over that list — the
  `DEFERRED.md` row whose stated trigger is ~10⁴ reactions, which this corpus exceeds by three
  orders of magnitude.
* `ingest_reaction` ends in `propose_note` unconditionally: one git branch per reaction, through a
  gate a human is supposed to read.
* `sync_entries` calls `_merged_note_bodies()`, loading every merged note body once per run.
* A corpus release is a versioned *load* addressed by key. "Everything since Tuesday" is not
  meaningful for it, and the `ElnAdapter` cursor contract is datetime-shaped.

Declaring no ingest half sidesteps all five with **zero edits** to any of them. Note what that
makes unnecessary: there is no "publish mode" flag on `ingest_reaction`, because there is no ingest
path to gate. The alternative design — a mode switch on the ELN path — would have added a branch to
the one function every reaction in this system passes through, to express a distinction that is
better expressed by not being on that path at all.

### The line is what the record *is*, not how much of it there is

An ELN entry is a claim this organisation makes about an experiment it ran. It belongs in the
knowledge graph, behind the PR-gate, as a note a human signed off. A patent reaction is literature:
it is *evidence*, it cites a document anyone can read, and no one here is going to review thirteen
million of them. `D-2026-08-06` drew that line for documents on a mounted share — "indexed as cited
evidence rather than PR-gated notes" — and this is the same line for reactions.

So a corpus row's `citation` is a patent number, not a note id, and the retriever cites
`pistachio:US9376441B2`. `kg-validate` never sees it, because it never enters the graph.

### The schema is a binding, again

The site's loaded Pistachio schema is not visible from here, and a schema nobody can see cannot be
written into Python — the argument `D-2026-08-04` makes for the ELN, unchanged. So `WarehouseBinding`
gains a third section, `corpus:`, beside `ingest:` and `vector:`, and it is far simpler than the
ELN's: a corpus hands us the reaction *already assembled* as `reactants>agents>products`, so there
is no `related:`, no `components:` and no `impurities:` block. The species come from splitting the
SMILES; their refined roles come from the labeller, which is the entire point of the label index.

Pagination is **keyset, not a datetime cursor and not `OFFSET`**. `OFFSET n` on a multi-million-row
table makes the warehouse walk and discard n rows per page, so a drain gets quadratically slower
exactly as it gets further in. Resuming strictly after the last key seen is an index seek, and it
makes a stopped drain resumable at no cost — which matters, because every write is an id-keyed
upsert of the record phase, so re-draining an unchanged release is a no-op that *keeps the labels*.

### Its molecules are a second table

`corpus_molecules` (`infra/sql/052`) carries the same five columns `molecule_fingerprints` does, so
`PostgresFingerprintStore` serves similarity over it with no new code. It is a second table rather
than more rows in the first because the two answer different questions and cite different things:
one is "have we made this?" and its hits cite a compound note, the other is "is there literature
precedent?" and its hits cite a patent. Merging them would swamp the ELN corpus by four orders of
magnitude and hand `similar_molecules` millions of hits whose `compound_note_id` resolves to
nothing.

It carries one column its sibling does not: `pattern_bits`, an RDKit pattern fingerprint as set-bit
indices, with a GIN `@>` index. That is what makes substructure search over a corpus this size
possible at all — `find_substructure_matches` loads up to 5,000 rows and matches each, which at
this scale does not make the answer slower, it makes it *wrong*. The screen is sound in one
direction (a molecule missing any of the query's bits provably cannot contain it), which is exactly
what a prefilter may be, and the survivors are verified exactly. ECFP bits cannot do this;
`DEFERRED.md` says so and the reason is that Morgan hashes whole circular environments.

## Consequences

* Pistachio ships as `src/chemclaw/ingest/sources/pistachio/datasource.yaml` — one folder, one
  file, no Python — and ships **disabled**, so a cluster reads it only by naming it in
  `CHEMCLAW_DATA_SOURCES` and pointing the binding at a table it already has. This repository holds
  no address, no credential and no vendor client.
* `tests/test_reaction_corpus.py` asserts, with the source enabled, that it is **not** in
  `active_ingest_source_names()`. That is what keeps `read_corpus` and the O(n²) clustering away
  from it as a checked property rather than a comment somebody could delete. The `DEFERRED.md`
  "universal ingest abstraction" row is unchanged and is not closed by this: the seam was sidestepped
  a second time, not widened.
* The whole path is proved offline against `tests/warehouse_fake.KeysetWarehouse`, the way
  `eln-snowflake` shipped and was proved before any tenant existed. When the real table arrives the
  only thing that has to be right is the column names in that one YAML file.
* `make datasource-validate` gained a two-way check: a `labels: provides:` naming a group the
  `corpus:` binding maps no column for is refused, because the coverage report would otherwise
  repeat that claim to a chemist. A `labels:` block on a source that contributes no reactions at all
  is refused for the same reason — it looks like labelling is configured when nothing is.
* One bug the offline path caught before any tenant saw it: `as_text` is `str()` for everything, so
  a NULL `NAMERXN_NAME` was being stored as the four-character string `"None"` — and would then have
  been counted in frequency tables beside the real named reactions.

## Two seams, one table

`D-2026-08-25-a-lakehouse-arrives-on-two-seams-not-one` attached Pistachio independently and on the
same day, as a `vector:` binding searched by embedding similarity. Both landed in one folder, and
merging them was not a tie to break: **they answer different questions of the same table.**

`vector:` ranks by embedding similarity — "find me reactions that read like this one" — which is
what a research sweep wants and what `gather_evidence` reaches for. It cannot answer *which ligand*,
*as what*, *under what conditions* or *how it was worked up*, because those are properties of the
recipe and an embedding of the reaction text is not a queryable decomposition of it. `corpus:` is
drained into the label index, which is exactly that decomposition.

So one `pistachio` source declares both blocks over one connection, and its single `retrieve:`
callable is the vector retriever. The consequence worth recording is in the *drain*, not the
manifest: `corpus_sources()` reads the binding off the **manifest**, not off the built retrieve
half. A source declares exactly one retrieve callable, so "which sources have a corpus" can never be
answered by asking what that half happens to be an instance of — the first version of this code did
ask that, and it stopped finding Pistachio the moment the vector seam claimed the slot.
`tests/test_reaction_corpus.py::test_one_source_carries_both_seams_onto_the_same_table` is what
keeps the two pointed at one relation.
