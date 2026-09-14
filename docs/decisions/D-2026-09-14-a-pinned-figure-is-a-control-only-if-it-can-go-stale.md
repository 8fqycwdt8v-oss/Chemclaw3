# D-2026-09-14-a-pinned-figure-is-a-control-only-if-it-can-go-stale — three figures in the readiness record, and what pinning one buys

**Status**: accepted. Amends §3 of `D-2026-09-14-what-a-deployment-team-is-getting` in place, on
that document's own contract and for the reason
`D-2026-09-14-tools-were-never-the-variable` gives for amending it rather than superseding it.

## Context

§3 of the readiness record is headed *"Measured — a number somebody can reproduce"*, and the
document's preamble says the figures there *"describe one execution somebody can repeat, and each
names the ADR or the target that produced it"*.

Its first row now carries three figures from the three-arm run that corrected the ChemBench
attribution: **62 → 58** with the prompt held fixed, **58 → 76** with the tools held at zero, and
**p = 0.00012**. A review asked where a reader would go to reproduce them.

## What was measured

`git grep 0.00012` returns three restatements of one claim and **no artefact**. Nothing under
`tasks/` holds a benchmark run's output, for that run or any other. The ADR that recorded the
figures says so itself: the table is *"the review's run, not this one's"*, and re-running it needs a
gateway whose credential here answers **HTTP 400, "credit balance is too low"**.

The contrast is one row down in the same table. *"331 probes"* is backed by 331 committed
transcripts under `tasks/live-test/transcripts/corpus/`, which is what lets a reader check the claim
rather than believe it.

And `tests/test_readiness_record.py::test_the_external_benchmark_number_is_still_in_it` pinned
**`"62 → 58"` as a literal**.

## The decision, and the option not taken

**The artefact is not shipped, because it cannot be.** Producing it needs a model gateway with a
balance, which this environment does not have; writing the transcripts by any other means would be
manufacturing the evidence the row is missing, which is worse than the gap and is the exact failure
this programme has spent ten waves correcting.

So the row **says where its figures came from**: a review session's run, against a gateway this
environment cannot reach, with no transcripts committed — stated beside the fact that both halves
are one run away for anybody with a balance, since the corpus is vendored and keyed and
`data/evals/profiles/tools-removed.yaml` is registered by `infra/live/processes.sh`. **Repeatable**
and **reproduced-here** are different claims and the section's heading promises the weaker one; the
preamble now says a row owes a reader the difference.

**And the pin is removed, because it was not a control.** A literal `62 → 58` in a test cannot
notice that figure going stale — the only thing it can detect is the document no longer repeating
it. It does not detect staleness; it *enforces* it, and the first person to re-run the arm with a
balance would have had to edit the control in order to record their measurement. That is the shape
`D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` warns about, with the number moved into
a test where it looks safe.

What the assertion was actually for survives and is strengthened: the *correction* must stay in the
record beside the flattering half. So the test now reads the ChemBench row itself and requires it to
cite the ADR, to name the system prompt as the variable, and to say the three-arm run's record is
not in this tree. `62/100` and `74/100` stay pinned, because those two are what `make
live-benchmark` produces from a corpus this repository vendors with a checksum.

## The same rule, applied the other way, one row down

The live-run row *is* backed by its evidence, and it shipped saying **27 distinct tools exercised**
where the 331 committed transcripts hold **26** across `tools_called ∪ tools_failed ∪ tool_results`.
Nothing counted them. That is the case where digits belong in a test, because the test can fail as
they go stale: `test_the_live_run_row_counts_what_its_transcripts_hold` derives both the probe count
and the tool count from the transcripts and reads them off the row.

It also refuses a **second** committed run rather than unioning it in. The row describes one
execution on one date; a check that quietly averaged two would let it go on describing the first,
which is the failure one row up in a different costume.

## Consequences

- A figure belongs in a test when the test fails as the figure goes stale. Where the underlying run
  cannot be repeated in the tree, what the test can hold is the *claim* — which ADR, which variable,
  and that the run's record is elsewhere. Where it can, the test holds the digits, and the two rows
  of §3 this record touches are one example of each.
- Nothing about the ChemBench result's substance changes. It is still the only external number here,
  it is still the one that does not flatter the system, and it is still stated first.

## What keeps it true

- `tests/test_readiness_record.py::test_the_external_benchmark_number_is_still_in_it` — the pair,
  the ADR, the variable and the provenance caveat, all read off the ChemBench row rather than off
  the whole document.
- `tests/test_readiness_record.py::test_the_live_run_row_counts_what_its_transcripts_hold` — the
  other direction: the live-run row's probe count and tool count derived from the committed
  transcripts, and a second committed run refused rather than absorbed.
- `tests/test_readiness_record.py::test_every_test_the_readiness_record_names_exists` — the
  record's own rule, unchanged: a clause may only name a control that is there.
