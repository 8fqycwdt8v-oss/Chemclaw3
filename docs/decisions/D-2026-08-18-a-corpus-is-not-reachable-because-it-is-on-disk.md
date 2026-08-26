# D-2026-08-18-a-corpus-is-not-reachable-because-it-is-on-disk — the seeded corpus is checked by value, and what cannot arrive is declared

**Status:** accepted
**Context:** extending the four-repo live lane (`infra/live/e2e-full-stack/`) to the data itself

## Context

The 2026-08-17 full-stack run reported ingestion as proven with this evidence:

| observation | value |
| --- | --- |
| `note_proposals` after the ELN sync | **638** |

638 is a count. A count cannot say *which* records arrived, and it cannot say anything at all about
whether the numbers in them are the numbers in the paper. Asking those two questions found three
things the count could not have shown, all of them in data everybody believed was tested:

1. **57% of the seeded ORD corpus cannot be ingested, and never could.** `Chemclaw3_mock` seeds
   10,011 ORD records; 4,251 map, 5,760 are refused. Every refusal is the Perera flow-Suzuki screen
   (*Science* 2018, 359, 429), whose second coupling partner the source spreadsheet publishes only
   as its own shorthand — `2a, Boronic Acid`. `ord_adapter._smiles` refuses it rather than
   inventing a structure, which is correct and is pinned by
   `test_ord_compound_with_no_resolvable_identifier_is_still_refused`. What was wrong was that
   nothing above the adapter knew.

2. **Two grounded probes were graded against data that cannot exist, and both scored the truthful
   answer as a failure.** `gr-03` asked "have we ever coupled anything like `Brc1ccc2ncccc2c1`" —
   a halide occurring in 1,152 records, all of them in the refused screen — and listed
   *"'no prior work found' or equivalent"* under `forbids_claims`. "No prior work found" was the
   correct answer. `gr-08` asserted that a 119.43% yield "is real, is in the corpus"; `OrdReaction`
   bounds a yield at 100, so ingest rejects that record every run (measured: `ingested=31
   rejected=1`, one WARNING naming it). Both probes rewarded a model for describing records it
   could not see.

3. **The corpus had to be backfilled before any of this was visible.** Every ORD export shares one
   mtime — the moment the repo was cloned — and carries an older payload timestamp, so the
   incremental cursor passes all of them on its first firing and no later run can qualify them
   again. The harness never took the remedy its own WARNING names.

None of this was reachable by reading code. The refusal is documented, deliberate and tested; the
probes are careful and specific; the sync is correct. Each piece is right and the composition lost
more than half the data, silently.

## Decision

**`make live-data` checks the seeded corpus by value against the published tables, and every
dataset declares whether it can arrive.**

`Chemclaw3_mock` commits the raw factor tables it expands into ORD records — the CSVs in
`app/eln/real_data/`, pulled once from the papers. Those are the ground truth, because they sit
upstream of both the mock's seeding code and this repo's adapter: a check against them cannot be
satisfied by two stages agreeing on the same mistake. `cli/live_data.py` follows every published
row down the pipeline

    published (CSV) -> seeded (ORD JSON) -> mapped (OrdReaction) -> note -> proposal

comparing multisets of `(dataset, factors…, yield)` at each hop against the CSV rather than against
the stage before it.

The declaration is the half that makes it a regression detector rather than a number to admire.
Each `Dataset` states `reachable`, and the check fails **in either direction**: a reachable dataset
that stops mapping is an ordinary regression, and a dataset declared unreachable that starts
mapping means somebody taught the adapter to invent a structure the source never published — which
would propagate into a fingerprint index, a similarity hit and eventually a proposed note.

Two consequences follow directly, and both are taken here:

- `infra/live/e2e-full-stack/up.sh` starts the epoch backfill on bring-up, so the seeded corpus is
  reachable at all. It waits 120 s and no longer: every proposal costs a PR-gate git branch and
  commit, measured at **1.81 s/record**, so the full corpus takes a little over two hours and a
  bring-up must not block on it. A drain still running is reported, not failed.
- `data/evals/probes/grounded.yaml` states what is reachable, separately from what is on disk.
  `gr-03` moves to 4-bromoanisole (264 records on the reachable Buchwald-Hartwig plates); `gr-08`
  becomes bucket B and now measures whether a model admits it cannot find the well it was asked
  about, which is the harder question and the one with a true answer.

## Consequences

- The rule the lane leaves behind, which outlives the five datasets it currently binds:

  > **A corpus is not reachable because it is on disk.** Seeding, mapping, proposing and indexing
  > each drop records for their own good reasons, and every one of those reasons is correct in
  > isolation. Only an end-to-end count *per dataset*, against the published source, says what
  > survived — and a probe suite grounded in "what the mock seeds" is grounded in the wrong number.

- The 5,760 refused records are now a declared, measured fact rather than an accident. Whether to
  recover them is a separate and open design question — `_smiles`' own docstring already calls this
  "57% of a real corpus lost, including the yield data on components that *were* resolvable" — and
  it is filed in `docs/planning/BACKLOG.md` with the two candidate shapes. This ADR deliberately
  does not settle it: the point of the measurement is that the decision can now be taken against a
  number instead of an impression.

- `live_data` is a lane, not a test, for the same reason `live_jobs` is: it needs a seeded checkout
  of a sibling repo. `tests/test_live_data.py` pins the part that decides whether a green result
  means anything — that a 0.00% yield reads as a measurement rather than as silence (644 of the
  seeded corpus are exactly 0.00%, and the first draft of this lane's own verification script had
  that bug), that a blank cell in a published table compares equal to an omitted reagent (480
  no-ligand and 720 no-base *control* conditions in one screen), and that the unreachable-dataset
  check fails when such a dataset starts being accepted.

## Alternatives considered

**Grade the corpus through the probe suite instead of a separate lane.** Rejected on the evidence
above: it is what was being done, and it conflates a corpus that never held the data with a model
that did not look for it. The 2026-08-17 grounded results are uninterpretable for exactly this
reason — they were graded against a corpus holding none of the ORD data they name. `live-probes`
becomes meaningful only once `live-data` is green, which is the same ordering `live-jobs` has to
`live-probes` for the durable half.

**Assert per-dataset row counts in the lane.** Rejected: a hard-coded count is a second source of
truth that drifts, and the published CSV is right there. The counts in this ADR are measurements,
not constants — the code reads the tables.

**Teach `_smiles` to accept a structure-less component so the flow-Suzuki set ingests.** Not
rejected, but deliberately not decided here. It is a change to what a `Component` *is* — every one
carries `smiles: str` today, and the reaction SMILES, the fingerprint index and the similarity
search all read it — so it wants its own ADR and its own measurement of what a partially-structured
reaction does to retrieval. Making the loss visible is the prerequisite, not the fix.
