# Fixing four defects the review found in the protocol condenser

Plan: `/root/.claude/plans/as-if-you-ask-fuzzy-crab.md` (approved).
Base: `737124d` (PR #206, merged). Branch: `claude/condenser-review-fixes`.

## Measured before fixing (from the review)

- [x] **Fabricated changes.** Three runs, identical conditions, one extraction failure →
      `solvent 2-MeTHF → —` then `solvent — → 2-MeTHF`. Two swaps that never happened.
      A share document beside reaction notes → `temperature 90 °C → —; time 12 h → —` and back.
- [x] **Budget under-count.** A realistic chunk with conflicts + provenance: 300 counted,
      569 serialized — **47%** of the real payload uncharged.
- [x] **Read ceiling after materialisation.** `read_document` fetches every row and joins the whole
      document, then trims. The setting's comment claims it prevents the pull.
- [x] **Registry rebuilt per ref.** 12 refs → **12** rebuilds; up to 24 per call.

## Steps

- [ ] 1. `_changes` compares only fields both sides recorded; three cell states (—/unchanged/list).
- [ ] 2. `_within_budget` charges the serialized chunk.
- [ ] 3. Bound the document read at the fetch (`max_chars` on `stored_document`, both backends;
      `StoredDocument.truncated`; `join_chunks` early stop).
- [ ] 4. Hoist the source registry out of the resolution loop.
- [ ] 5. Minor: `_conditions` emptiness check; `truncated_by` first-hit comment.
- [ ] 6. BACKLOG row for the same latent issue in `changes_between`.
- [ ] 7. `tasks/lessons.md` — why homogeneous fixtures hid all of this.

## Review

_(pending)_
