# D-2026-08-25-the-structure-is-discarded-at-the-note-boundary — a protocol reaches the agent whole, and its figures reach it as figures

**Status:** accepted · **Date:** 2026-08-25

## Context

Asking for similar reactions returns many protocols. A protocol is atomic — an SOP is one procedure
and half of one is misleading rather than merely shorter — so the unit that has to fit a model call
is one whole procedure, and N of them do not fit. The question was how to condense them. Three
things turned out to stand between the system and that question, and only the third was the
condenser.

### The protocols often never arrived

`ingest/eln/note.py::_procedure_block` rendered a `## Procedure` section only `if reaction.steps`.
`ingest/eln/warehouse/binding.py` excludes `steps` from `_MAPPABLE_FIELDS` on the stated grounds
that "a warehouse records a protocol as prose, which lands in `procedure_text` verbatim". Both
statements were true and nothing rendered that prose.

Measured: a warehouse-shaped reaction carrying **251 characters** of procedure produced a
**63-character** note body containing none of it. `procedure_text` had three writers and exactly
**one** reader in the whole tree — a 240-character excerpt in `memory/optimization.py`. So for the
first live connector, a Snowflake ELN, `expand_note` answered with a reaction that had no recipe.

### The figures existed only as sentences

`OrdReaction` carries temperature, time, yield, purity, the impurity profile and the outcome class,
and it is **never persisted**. It exists transiently inside `durable/memory_jobs.py::read_corpus`,
which re-reads and re-maps the entire ELN from `datetime.min` on every call, on the background
worker, behind an ingest half `tests/test_datasource_isolation.py` proves the chat pod does not
import. `ElnAdapter` has no fetch-by-id. There is no reaction table — `reaction_fingerprints` holds
bits and a label.

So at turn time the numbers a chemist compares had been rendered into prose and the structure
thrown away. Anything comparing runs would have to re-derive them, with a model, from sentences the
ingest had just finished writing them into.

### For the share, a protocol was already split and unrecoverable

`ingest/documents/sync.py::_read_and_parse` hashes the parsed text into `doc_id` and then discards
the text; only chunk rows persist, cut at `chunk_chars`. `DocumentIndex` had no method that read
them back, there was no parent-document retrieval, and no tool fetched by `doc_id` — while
`ShareDocumentRetriever` had been citing `sharedrive:doc-9f2a…#3` all along. The invariant "one
protocol is never split in two" was simply false on that source.

## Decision

**A protocol is addressed by exactly the string it is already cited as.** A knowledge-graph note id,
or the share's citation with its `#ordinal` dropped. No new identifier: the reader was missing, not
the address. This is `D-2026-08-21-a-geometry-is-an-address-not-a-payload`'s shape one step out.

**`procedure_text` is rendered, and both are rendered when the steps are an independent account.**
The question — *are these steps a cut of this prose?* — is answered by containment rather than by a
tuned threshold, because measurement showed the two adapters differ:

| adapter | similarity of joined steps to prose | every step's text inside the prose |
|---|---|---|
| `json_adapter` (segments the prose) | 0.992 | yes |
| `ord_adapter` (maps structured ORD fields) | 0.555 | no |

The `ord_adapter` steps read `Add CCO` where the prose reads "a catalytic amount of sulfuric acid
over 30 min". Steps-only would have dropped that sentence — the same loss one source over. Both-
always would duplicate the entire recipe on every `json_adapter` note. Containment asks the question
that matters, needs no number, and cannot drift.

**The figures go into note frontmatter as `ProcessConditions`, not into a second table.** The
git-markdown graph stays the one source of truth (D-004), `expand_note` already returns frontmatter,
`kg-validate` already checks it, and there is no migration and no store to keep in step.
Note-type-specific on a shared model exactly as `compound_smiles`, `calc_refs` and `artifact_refs`
already are.

Exactly the columns the comparative table renders, and **no more** — this is not a serialization of
`OrdReaction`, which would be the second untyped schema `attributes` argues against. The species
sets behind "solvent DMF → 2-MeTHF" are deliberately absent: they need the full input list, and a
caller that wants them reads the prose, which is where the free-text half of a digest is looking
anyway. `valid_from` already carries the date the run was performed (D-162) and is not repeated.

**Two silences stay silences.** A note about no recorded run carries no block rather than
`conditions: {}`, which would claim the question was asked and answered emptily. And a successful
run does not assert its own success: `outcome_class` defaults to `SUCCESS` on every source that does
not report one, so writing it unconditionally would turn "the ELN did not say" into a claim that the
run worked — and a failure that reads as an ordinary run is the one row in a comparison nobody may
misread.

**A share document is reassembled from its chunks, never re-read from the file.** Three reasons, in
ascending force: the retrieve half deliberately imports nothing that can open a document and
`tests/test_datasource_isolation.py` holds that; the chat pod need not have the mount at all; and
decisively, `doc_id` is the hash of the text *as parsed at crawl time*, so re-reading can return
bytes the turn's citations no longer point into — `verifier.turn_evidence`'s "does this exist"
versus "did this turn see it", one level up.

**De-overlapping refuses an ambiguous match.** Two earlier rules deleted real text, both found by
measuring against the real chunker rather than reasoning about it:

| rule | `"x" * 5000` rebuilt as | period-10 CSV, 6,000 chars, rebuilt as |
|---|---|---|
| naive longest match | **2,600** | 3,200 |
| plus a one-character periodicity shift | 5,000 | **3,200** |
| positional uniqueness (shipped) | 5,000 | 6,000 |

`_split_block` carries an overlap and `_hard_split` does not, and repetitive content makes the two
indistinguishable from the content alone — a share allowlisting `.csv`/`.tsv` makes that ordinary
rather than exotic. When more than one alignment explains a match the repeat is left in place: a
bounded duplicate is a cosmetic wart, a dropped step is a wrong procedure.

**Merged notes converge through the existing sync.** `eln_sync` already supports an explicit `since`
backfill that touches no cursor, and `note_id_for_reaction` is idempotent, so a re-sync proposes the
same id with a corrected body through the ordinary PR-gate. No new command, and nothing rewritten
behind a reviewer's back.

## Consequences

- A Snowflake-ingested reaction now reaches `expand_note` with its recipe. Nothing else changes for
  a source that already populated `steps`.
- `major_impurity` moves from `memory/optimization` onto `OrdReaction`, because two consumers now
  ask it and "which impurity is the major one" is a property of the reaction rather than of either
  artifact.
- `DocumentText` declares `truncated` and says plainly that it returns the *indexed* text rather
  than the file's bytes: `chunk_document` strips pieces, drops empties and hoists coordinates, so a
  promise of byte-fidelity would be false.
- A whole-document read asks the share's entitlement gate, including reject-if-absent. It is a
  strictly larger disclosure than a ranked excerpt, so this is load-bearing rather than symmetric.
- Existing reaction notes carry no `conditions` block until their source entry is next synced, and
  human-authored reaction notes never will. The condenser handles both — a missing figure renders
  as `—`, which is `drop_empty_columns`' existing contract — so the corpus converges without a
  flag day.

## What was rejected

**Re-parsing our own rendered note body back into `OrdReaction`.** `note_from_ord_reaction` is not
the only producer of a `reaction` note — a human writes one with no blocks at all — a
renderer/parser pair is `D-2026-08-05-one-rule-in-three-places-is-three-rules`, and a silent
mis-parse yields a confident wrong number in the one artifact a chemist reads as the record.

**A derived `reaction_records` table.** It would also have given `read_corpus` a way out of its
full-rescan, which is a real benefit and a separate one. Rejected here because it stores the same
facts twice and needs a migration to answer a question the note can answer itself. The
full-rescan is left as its own problem rather than fixed as a side effect of this one.
