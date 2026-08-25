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
- [ ] 2. The per-protocol map (`agent/protocol_digest.py`), copying `verifier.py`'s five properties.
- [ ] 3. `condense_protocols` tool + profile + skills.
- [ ] 4. Compaction docstring + the "not a reversal" ADR.
- [ ] 5. The cap's currency (`gather_evidence_max_chars` + `EvidenceSweep`) — separable, own ADR.
- [ ] 6. Registers, `.env.example`, READMEs, DEFERRED rows.

## Review

_(pending)_
