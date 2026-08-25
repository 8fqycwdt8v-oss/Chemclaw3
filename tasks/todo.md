# Handling many free-text protocols in one turn

Plan: `/root/.claude/plans/as-if-you-ask-fuzzy-crab.md` (approved).

## Corrections folded in after approval

- **`OrdReaction` is unreachable at turn time.** It lives only behind `active_ingest_sources()`,
  which the chat pod deliberately does not import (`tests/test_datasource_isolation.py` asserts the
  separation in a subprocess). So the split is not "structured vs unstructured" — it is **"does the
  record reach *this process* as a model or as prose"**. The *reduce* (table, delta column, quality
  columns, ordering caveat) is deterministic and shared by both processes; the *map* is already
  written and LLM-free on the background worker, and is a bounded per-protocol extraction in the
  chat pod.
- **A reaction note is already one chunk per note** (`retrieval/retrievers.py:295` `_chunk_for`),
  excerpted to 240 chars. So a graph protocol is *shortened*, never *fragmented* — the "cannot be
  split" invariant already holds there. Defect 2 is the share only, as planned.
- **The whole-document address already exists**: it is the `source_note_id` the share retriever
  already emits (`sharedrive:doc-…#ordinal`) with `#ordinal` dropped. No new id scheme.
- `fan_out` is Temporal-only (`workflow.execute_child_workflow`) and unreachable from a tool — the
  turn-time map uses an `asyncio.Semaphore`.
- Frontmatter (1.2) stays as approved, with one caveat now known: `note_from_ord_reaction` is not
  the only producer of a `reaction` note — human-authored ones exist (`knowledge/reaction/*.md`,
  `created_by: human`) and carry no structured fields. So frontmatter helps the ELN-ingested
  majority and the prose map is needed regardless. Both halves were already in the plan.

## Measured

- [x] Warehouse-shaped `OrdReaction` (251 chars of `procedure_text`, no `steps`) renders a **63-char**
      note body containing **none** of the procedure. `## Procedure` absent. The drop was real.
- [x] `steps` vs `procedure_text` overlap: `json_adapter` 0.992 similarity (steps *are* the prose
      recut, every step's text verbatim inside it); `ord_adapter` 0.555 (steps read `Add CCO` where
      the prose reads "a catalytic amount of sulfuric acid over 30 min"). Containment separates the
      two with no threshold. Both branches now render correctly.
- [x] Reassembly: two de-overlapping rules **deleted real text** before the one that shipped —
      naive longest-match rebuilt a 5,000-char line of `x` as 2,600; a one-char periodicity shift
      still rebuilt a period-10 CSV line as 3,200 of 6,000. Positional-uniqueness loses zero on all
      three adversarial inputs, and real prose overlap returns at exactly its original length.
- [x] Both index backends return the identical stored document (real Postgres, PASSED not SKIPPED).
- [x] `conditions` frontmatter round-trips through `render_note` -> `read_note` exactly; the only
      field that differs is `body`, by the documented trailing-whitespace normalization.
- [x] `test_prompt_caching`'s two live failures are **pre-existing** — identical on `bed7d69`,
      an invalid live API credential.

## Steps

- [x] 0. Extract the reduce → `memory/comparison.py`; generalize `progression` over a narrow record
      shape. Acceptance: `tests/test_optimization.py` + `tests/test_progression.py` green **unmodified**.
- [x] 1a. Render `procedure_text` when `steps` is empty (`ingest/eln/note.py`).
- [x] 1b. Structured process fields in note frontmatter + `kg-validate` shape check.
- [x] 1c. Document reassembly + `read_document` behind the entitlement gate.
- [x] 2. The per-protocol map (`agent/protocol_digest.py`), copying `verifier.py`'s five properties.
- [x] 3. `condense_protocols` tool + profile + skills.
- [x] 4. Compaction docstring + the "not a reversal" ADR.
- [x] 5. The cap's currency (`gather_evidence_max_chars` + `EvidenceSweep`) — separable, own ADR.
- [x] 6. Registers, `.env.example`, READMEs, DEFERRED rows.

## Review

**What shipped**, in the order it had to happen:

1. `memory/comparison.py` — the comparative table extracted from `optimization_campaign_note`, so
   the turn-time artifact and the PR-gated campaign note are one renderer at two altitudes.
   28 existing tests pass unmodified, which is what makes it an extraction.
2. `procedure_text` is rendered when a source maps no `steps`, and both are rendered when the steps
   are an independent account rather than a cut of the prose — decided by containment, not a
   threshold.
3. `ProcessConditions` frontmatter: the figures a chemist compares reach the note as figures.
4. `reassemble.py` + `stored_document` + `read_document`: a share document is addressable whole.
5. `retrieval/condense.py` + `agent/protocol_tools.py`: many whole protocols become one comparison.
6. `gather_evidence` gains a character bound and a return type that can say it was cut.
7. Three ADRs, three registers, five READMEs/skills/profiles.

**The result, measured** (2.8 kB protocol, the middle of the 3–8 kB band):

| N | `expand_note` x N | `condense_protocols` | ratio |
|---|---|---|---|
| 5 | 3,600 | 472 | 7.6x |
| 20 | 14,410 | 1,648 | 8.7x |
| 80 | 57,650 | 6,352 | 9.1x |

Protocols that fit the 100k budget on tool results alone: 137 -> 1,455 (2.8 kB), 49 -> 1,455 (8 kB).
The structural property is that the condensation's marginal cost is **independent of protocol
size**, so the budget bounds how many protocols a turn can hold rather than how long they are.

**Four things measurement caught that reasoning had not:**

- The first de-overlapping rule deleted 2,400 of 5,000 characters; the second still deleted 2,800
  of 6,000 on period-10 content. Both silent. Only the third is correct, and both are pinned.
- The first `Condensation` serialized the table *and* the rows it was rendered from — 1.4x, which
  would not have been worth building.
- `rows` was in input order while `_table` sorted internally, so "changed vs previous" would have
  been a claim about a different row than the one above it.
- The fairness test passed against the mutant it was written to catch until it asserted both
  directions; it still does not catch a score-re-sort, and says so rather than reading stronger
  than it is.

**Not done, deliberately, with the reasoning recorded rather than the work half-done:**

- `read_corpus`'s full rescan (`BACKLOG.md`) — a derived store would have fixed it as a *side
  effect* of this change, and answering a scaling problem sideways is how a store nobody decided
  on gets built.
- Reagent/solvent *set* diffs in the comparison (`DEFERRED.md`) — needs `OrdReaction.inputs`, which
  the chat pod cannot reach; diffing free-text reagent lines would report a change whenever one
  procedure named a loading and its neighbour did not.
- A durable per-set digest (`DEFERRED.md`) — a stub for a refusal nobody has hit.

**Pre-existing failures, confirmed identical on `bed7d69`:** the two `test_prompt_caching` live
tests (invalid API credential, 401) and two `mypy --strict` errors in
`test_bo_campaign_record.py:498` and `test_step_handoff.py:376`.
