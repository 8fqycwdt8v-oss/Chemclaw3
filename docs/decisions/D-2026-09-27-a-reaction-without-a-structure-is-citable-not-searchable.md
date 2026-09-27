# D-2026-09-27-a-reaction-without-a-structure-is-citable-not-searchable — a record the source only half-drew is evidence, not a structure

**Status:** accepted · **Date:** 2026-09-27 · **Owner decision**, recorded as one: the owner chose to
keep these records and chose the tier; this ADR records that choice and the build it implies. Closes
the `BACKLOG.md` row *"5,760 ORD records — 57% of the seeded corpus — cannot be ingested at all"*
(issue #477).

## Context

`Chemclaw3_mock` seeds 10,011 ORD records from five published HTE screens. Measured on this branch's
base with the production `OrdJsonAdapter` over the mock's full seeding (`seed_all()`,
`MOCK_HTE_MAX_RECORDS_PER_DATASET=0`): **4,251 mapped, 5,760 refused**, and every refusal is the
Perera flow-Suzuki screen (*Science* 2018, 359, 429). Each of those records carries two species
with a `NAME` and no structure — the second coupling partner, which the source spreadsheet gives
only as the paper's shorthand (`2a, Boronic Acid`, `2b, Boronic Ester`, `2c, Trifluoroborate`,
`2d, Bromide`), and the product, named as a phrase. `ord_adapter._smiles` resolved SMILES, then InChI,
then a name the reagent table knows, and raised when none did — correctly refusing to invent a
structure, and in doing so discarding the yield, the ligand, the base and the catalyst of every one
of those runs, all of which the source *did* state.

The row named two shapes and left the choice open: (a) a `Component` that may carry a name instead
of a SMILES, with the reaction kept out of every fingerprint index, and (b) a separate lower-tier
record type that retrieval can cite and similarity cannot reach.

## Decision

**The owner decided to ingest these records as a lower tier — citable as evidence, excluded from
structure and similarity search — with the named-only species carried as the source named it and
never guessed into a structure.** The shape built is between the row's two:

1. **A species without a structure is its own type, in its own list.** `ingest/eln/ord.py` adds
   `UnstructuredComponent` (a `name`, the source's text verbatim, plus the role, amounts and
   attributes a `Component` carries — both now share `_Charged`) and
   `OrdReaction.unstructured: list[UnstructuredComponent]`. `inputs` and `outcomes` still hold only
   species with a structure. `OrdReaction.tier` is derived, never stored on the reaction:
   `RecordTier.CITATION_ONLY` when `unstructured` is non-empty, else `STRUCTURED`.

   Not (a), because every structural reader iterates `inputs`/`outcomes` and reads `.smiles` as a
   structure (`memory/chains.py`, `memory/progression.py`, `ingest/labels/record.py`,
   `ingest/eln/validate.py`, `record._scale`, …); an optional `smiles` would have handed each of them
   a decision it has no business taking. With the split, a loop over `inputs` still meets only
   structures. Not (b), because a second record type means a second store and a second citation
   path for what is the same transcription with one fact missing — it is still a
   `reaction_records` row, cited as `reaction-<source>.<id>`, expanded by the same `expand_note`.

2. **The one place a structure is assembled refuses.** `OrdReaction.reaction_smiles()` and
   `transformation_smiles()` raise `StructureNotGiven` (a `ChemclawError`) on a citation-only record.
   The structured subset is a reaction nobody ran; returning it would have fingerprinted it silently,
   and any path that reaches these methods by mistake now rejects one entry loudly instead.

3. **The adapter carries the name.** `ord_adapter._species` returns a `Component` when any
   identifier resolves (the same three exact lookups as before) and otherwise an
   `UnstructuredComponent` carrying the first `NAME`/`IUPAC_NAME` verbatim. A compound with neither a
   structure nor a name is still refused. A workup reagent known only by name is still refused too:
   it is not a reaction species and carries no tier, and widening what a step may hold is not this
   decision.

4. **Ingest stores the record and indexes nothing structural.** `ingest_reaction` writes a
   citation-only record to `reaction_records` and **no** `reaction_fingerprints`, molecule-fingerprint
   or `reaction_labels` row — the molecule rows too, although each named structure is real, so the
   rule is one a test can hold ("a citation-only record contributes no row to any fingerprint
   store"), and the label row because the labeller derives an atom mapping and a named reaction from
   `record_smiles`, which a partial reaction would turn into an inference. `validate_ord` still
   parse-checks the structures given and skips the mass balance, which is uncheckable either way (a
   named input may supply any element; a named product contains elements nobody can list).

5. **The tier is a column, and the structural readers ask it.** `infra/sql/110_reaction_record_tier.sql`
   adds `reaction_records.tier` (`structured` | `citation-only`, default `structured` — true of every
   existing row, since the only ingest path refused such a record, and true of what the previous
   image writes during a rollout). `ReactionRecord.tier` carries it and the upsert refreshes it, so an
   amended entry moves between tiers with its body. `ReactionRecordStore.structurally_withheld(refs)`
   answers "withdrawn **or** citation-only" over a page of hits, and replaces `retracted` in the two
   structural readers that asked it — `FingerprintReactionRetriever` (now on both its filtered and
   unfiltered paths) and the `rxnfp` `similar_reactions` tool. That clause fires for one case only: an
   entry ingested structured and later amended to name a species without its structure, whose old
   fingerprint row the app role cannot `DELETE`.

6. **The chemist is told.** The record body opens with *"Structure not given by the source for N
   species … so this record is citation-only"*, carries no reaction SMILES, and lists every species
   under `## Species` — a structure as its SMILES, a named one as the source's text followed by
   *"structure not given by the source"*. `expand_note` prepends a system notice outside the framed
   source text: cite it for what it states, infer no structure for a named species, and read its
   absence from a structural answer as saying nothing about it.

7. **Counts distinguish the tiers.** `IngestSummary.citation_only` (a subset of `ingested`, not a
   fourth outcome), `ElnSyncOutcome.citation_only`, the `ingest.finished` log field and
   `chemclaw_ingest_citation_only_total{source}`. `durable/memory_jobs.read_corpus` leaves the tier
   out of the memory corpus — every miner reading it is structural — and counts it without marking
   the read incomplete. `make live-data` declares the flow-Suzuki dataset `tier=CITATION_ONLY`
   instead of `reachable=False`: its records must map, all citation-only, the published shorthand
   must arrive verbatim (multiset equality against the CSV column), and none may hold a fingerprint
   or label row; a record of it arriving *structured* is the red case, because only an invented
   structure gets there.

Retention and erasure read no column this adds: `reaction_records` stays unpruned
(`durable/retention.py`) and the row's key is unchanged.

## Measured

The production adapter and `sync_entries` (in-memory stores) over the mock's full seeding, before
and after, with a per-record hash of the rendered record's pre-tier fields, the fingerprint input
and the label record phase:

| | before | after |
|---|---|---|
| mapped | 4,251 | 10,011 (4,251 structured, 5,760 citation-only) |
| refused | 5,760 | 0 |
| the 4,251 — record / fingerprint input / label phase changed | — | 0 / 0 / 0 |
| reaction-fingerprint rows | 4,251 | 4,251, none citation-only |
| label rows | 4,251 | 4,251 |

## Consequences

- **A citation-only record can be cited and cannot yet be found.** Every path that *finds* a reaction
  record starts from structure, so these are reachable by citation (`expand_note`, `kg-validate`,
  a campaign or protocol comparison citing one) and by no question. That is the tier working as
  decided, and it is also most of the value left on the table: "which base wins on
  6-chloroquinoline" is exactly what these runs answer. A `BACKLOG.md` row carries it.
- **An amendment into the tier leaves a label row behind.** `reaction_labels` is INSERT/UPDATE-only
  for the app role, so the facet tools can still count a run amended from structured to
  citation-only; the fingerprint half of the same residue is what `structurally_withheld` guards. A
  `BACKLOG.md` row carries it, with `protocol_design_tools.uncited_precedent`, which asks neither
  question.

**Revisit when:** the label index can hold a row with named-only species and no derivation — at
which point the citation-only tier can join the facet questions without joining structure search,
and step 4's exclusion of the label row should be re-decided (`science/labels/records.py`'s
`SpeciesLabel` is the file that would show it).
