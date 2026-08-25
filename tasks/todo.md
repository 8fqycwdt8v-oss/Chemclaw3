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

- [x] 1. `_changes` compares only fields both sides recorded; three cell states (—/unchanged/list).
- [x] 2. `_within_budget` charges the serialized chunk.
- [x] 3. Bound the document read at the fetch (`max_chars` on `stored_document`, both backends;
      `StoredDocument.truncated`; `join_chunks` early stop).
- [x] 4. Hoist the source registry out of the resolution loop.
- [x] 5. Minor: `_conditions` emptiness check; `truncated_by` first-hit comment.
- [x] 6. BACKLOG row for the same latent issue in `changes_between`.
- [x] 7. `tasks/lessons.md` — why homogeneous fixtures hid all of this.

## Review

All four defects fixed and each pinned by a test that **fails against the merged code**:

| defect | before | after |
|---|---|---|
| fabricated changes | `solvent 2-MeTHF → —`, `solvent — → 2-MeTHF` | `unchanged`, `unchanged` |
| budget under-count | 47% of the payload uncharged | 0% |
| read ceiling | 16 of 16 pieces fetched past the ceiling | 3 pieces, `truncated=True` |
| registry rebuilds | 12 for 12 refs | 1 per call |

Two things worth carrying:

- The third cell state (`—` for nothing comparable) was not in my first attempt at the guard.
  Silencing the fabricated change without it would have turned every incomparable pair into a
  positive assertion of sameness — a quieter version of the same lie.
- `test_a_real_condition_change_is_still_reported` exists because the other three pass trivially
  if the column says nothing at all. Deleting the comparison outright would have satisfied every
  test written for the defect.

Deliberately not fixed: `changes_between` has the same absent-is-a-value defect, bounded by its
homogeneous input. It is a `BACKLOG.md` row, because closing it means changing merged
campaign-note output and its tests — one rule instead of two is right, but not smuggled in here.

Suite: 4,299 passed, 3 skipped (shallow git history only; no Postgres skips). `make lint type`
clean. All four validators green.
