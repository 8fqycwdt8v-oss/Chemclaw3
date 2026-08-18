# Corpus fidelity — following the seeded data down the pipeline, by value

Extends `full-stack-e2e-2026-08-17.md`, which brought the four-repo stack up and proved every
*capability* reachable. This pass asks the question that one could not: **is the number in the
answer the number in the paper?**

Its predecessor reported ingestion as proven with a single observation — "638 note proposals". A
count cannot say which records arrived, and it cannot say anything about whether their values
survived. Asking both questions found that **57% of the seeded corpus had never entered the system
at all**, and that two probes written to be grounded in that data were scoring the truthful answer
as a failure.

## Headline

| | |
| --- | --- |
| Published measurements verified present, exactly once, unchanged | **9,987 / 9,987** |
| Conditions recovered from procedure prose, exactly | **12 / 12** |
| Seeded ORD records the adapter can map | **4,251 / 10,011** (57% refused) |
| Zero-yield records preserved as `0.0` rather than as silence | **644 / 644** |
| Grounded probes whose ground truth could not exist | **2 of 36** |
| Cost of one PR-gate proposal, measured | **1.81 s** |

## The ground truth is the paper, not the previous stage

`Chemclaw3_mock` seeds ~10,000 ORD records from real, published, cited HTE screens, and commits the
raw factor tables it expanded them from as CSVs in `app/eln/real_data/`. Those CSVs sit upstream of
both the mock's seeding code and this repo's adapter, so a check against them cannot be satisfied
by two stages agreeing on the same mistake. Every assertion below compares against the CSV, never
against the stage before it.

`make live-data` (`src/chemclaw/cli/live_data.py`) follows every published row down

    published (CSV) -> seeded (ORD JSON) -> mapped (OrdReaction) -> note -> proposal

and compares multisets of `(dataset, factors…, yield)` at each hop.

## What the corpus half found

```
| dataset                          | published | seeded | mapped | refused |
| bh_amination_hte                 |      3955 |   3955 |   3955 |       0 |
| suzuki_miyaura_flow_hte          |      5760 |   5760 |      0 |    5760 |
| santanilla_amidation_screen      |        96 |     96 |     96 |       0 |
| santanilla_sulfonamidation_screen|        96 |     96 |     96 |       0 |
| nielsen_deoxyfluorination        |        80 |     80 |     80 |       0 |
```

17/17 checks pass, in ~7 s with no infrastructure at all (`--corpus-only`).

**The mock's seeding is exact.** All 9,987 published measurements are present exactly once with
their factors and yields intact; nothing unpublished appears. That includes the parts most likely
to be lost in translation:

- **644 records at exactly 0.00% yield** arrive as `0.0`, not as absence. This is a real result — a
  base/ligand combination that failed — and a truthiness test anywhere on the path silently
  converts it into "unknown". Not hypothetical: the first draft of this pass's own verification
  script had `rx.yield_percent or -1` in it and mis-reported 21 of 400 records before the bug was
  found. `tests/test_live_data.py` pins the invariant.
- **480 no-ligand and 720 no-base control conditions** in the flow-Suzuki screen arrive as omitted
  inputs, matching the blank cells in the published table. The first version of the checker raised
  `KeyError` on them and could not read a fifth of that dataset.
- **The Suzuki yields are rounded to 2 dp on the way in** (`4.76410921845962` → `4.76`), which is
  ORD's reporting precision and correct for a UV-area yield. The lane pins the rounding per dataset
  rather than loosening its tolerance globally, so seeding the mass-ion column instead — or
  truncating further — still fails.

**And 5,760 records cannot be ingested.** Every refusal is the Perera flow-Suzuki set (*Science*
2018, 359, 429), whose second coupling partner the source spreadsheet publishes only as its own
shorthand: `2a, Boronic Acid`. `ord_adapter._smiles` refuses rather than inventing a structure,
which is correct and is pinned by a test that names this exact case. What was missing is that
nothing above the adapter knew, so 57% of the corpus was assumed present by everything downstream.

Worth recording precisely, because the tree already believed it had fixed this: `_smiles`' own
docstring cites the same measurement as the motivation for widening the identifier union to INCHI
and NAME. The widening is right in general. On this corpus it moved the number from **5,761 refused
to 5,760** — the shorthand is unresolvable by construction, which is exactly why the mock publishes
it as a name rather than guessing.

## The free-text half: the one place a value is derived rather than copied

The ORD checks above all ask whether a number *survived a hop*. The free-text corpus asks something
harder, and the mock is built to ask it: its 32 procedures come in pairs, and the two halves carry
their conditions differently.

```
uspto-suzuki-biphenyl-1   prose: "stirred at 82 °C for 4.0 h"   temperature_c/time_h: absent
uspto-suzuki-biphenyl-2   prose: "stirred under nitrogen"        temperature_c/time_h: 82.0 / 4.0
```

The `-1` half has **no structured value to fall back on**. If the extraction fails, the condition
is simply gone, and nothing downstream can distinguish "ran at 82 °C" from "temperature
unrecorded". Measured across every procedure that states both in prose: **12/12 recovered exactly**,
including `uspto-reductive-amination-1`, which runs at `0 °C` — the value a truthiness test loses.

Two claims in `grounded.yaml`'s header were checked against the fixtures rather than taken on
trust, and both are true: the `-2` records really do drop temperature and time from their procedure
text, and the Suzuki twins really do differ by an impurity list (`-1` carries des-methoxy biphenyl
at 1.4 area%, `-2` carries none) on otherwise identical charges. `gr-06` and `gr-07` are grounded in
data that is there.

## Two probes were grading against data that cannot exist

`data/evals/probes/grounded.yaml` promises in its header that "a miss is a defect rather than an
unanswerable question". For two of its 36 probes that was false, and in both cases the probe
punished the correct answer.

**`gr-03`** asked *"have we ever coupled anything that looks like `Brc1ccc2ncccc2c1`?"* and listed
`"'no prior work found' or equivalent, when over a thousand records on this exact halide are
seeded"` under `forbids_claims`. That halide occurs in 1,152 records and **nowhere else in the
corpus** — all of them in the refused screen. "No prior work found" was the truthful answer, and the
probe scored it as a retrieval defect while rewarding a fabrication. Re-targeted to 4-bromoanisole
(`BrC1=CC=C(OC)C=C1`), 264 records on the reachable Buchwald-Hartwig plates, with the chloride and
iodide siblings on the same grid — the same question, against grounding that exists.

**`gr-08`** asserted of a 119.43% yield that *"the value is real, is in the corpus"*. It is not:
`OrdReaction` bounds `yield_percent` at 100, so ingest rejects `santanilla-orgsyn-boronate-well-Y36`
on every run. Measured this pass: `eln sync: ingested=31 rejected=1`, with one WARNING naming the
record and quoting the validation error. The probe's `forbids_claims` then ruled out the only
honest answer available. Reframed to bucket B: it now measures whether a model faced with a value a
colleague quotes and the corpus does not hold **says so**, instead of explaining a record it never
saw. That is the harder question, and it has a true answer.

The file header now separates *on disk* from *reachable*, and `make live-data` re-measures both
every run — failing if either changes in either direction — so the comment cannot go stale the way
the counts above it did.

## The corpus had to be backfilled before any of this was visible

Every ORD export shares one mtime — the moment the repo was cloned — and carries an older payload
timestamp, so the incremental cursor passes all of them on its first firing and no later run can
qualify them again. Chemclaw3 detects this exactly right and loudly (`warn_late_arrivals`, one
aggregated WARNING naming the remedy); nothing took the remedy. `up.sh` now starts an epoch
backfill on bring-up.

It waits 120 s and no longer, because the drain is slow and the reason is worth writing down:

> **A PR-gate proposal costs 1.81 s**, measured at 103 records per 3.1 minutes, steady. The cost is
> the git branch-and-commit cycle, not the mapping — the whole 10,011-record corpus *maps* in 0.3 s.
> That is a little over two hours for the mock's 4,251 ingestible records, and 4,251 branches in the
> note repository.

A real deployment's first sync is a decade of records, where that is days. Nothing is broken — every
proposal genuinely is a reviewable unit — but a backfill and an incremental sync plausibly want
different submission shapes. Filed in `BACKLOG.md` rather than fixed here.

## What is new

| | |
| --- | --- |
| `src/chemclaw/cli/live_data.py` | the lane: binding, checks, epoch backfill, ledger |
| `tests/test_live_data.py` | 9 tests pinning the invariants that make a green run mean something |
| `make live-data` | runs it (`ARGS="--corpus-only"` needs no infrastructure; `--backfill` re-drains) |
| `infra/live/e2e-full-stack/up.sh` | starts the epoch backfill on bring-up |
| `data/evals/probes/grounded.yaml` | `gr-03` re-targeted, `gr-08` reframed, header states what is reachable |
| `docs/decisions/D-2026-08-18-a-corpus-is-not-reachable-because-it-is-on-disk.md` | the decision |

Three `BACKLOG.md` rows opened (recover the flow-Suzuki set; make an ingest rejection answerable;
the PR-gate's per-note cost) and one deleted — the ORD backfill row this pass closes.

## The rule

> **A corpus is not reachable because it is on disk.** Seeding, mapping, proposing and indexing each
> drop records for their own good reasons, and every one of those reasons is correct in isolation.
> Only an end-to-end count *per dataset*, against the published source, says what survived — and a
> probe suite grounded in "what the mock seeds" is grounded in the wrong number.
