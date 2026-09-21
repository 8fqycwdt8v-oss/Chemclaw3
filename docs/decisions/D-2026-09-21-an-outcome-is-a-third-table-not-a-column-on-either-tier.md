# D-2026-09-21-an-outcome-is-a-third-table-not-a-column-on-either-tier — closing the loop between a designed plate and what it produced

**Status:** accepted · **Date:** 2026-09-21 · Closes the `BACKLOG.md` row opened by
`docs/archive/IDEATION-2026-09-20-process-development-hte-and-protocol-prediction.md` §3.3, whose
row is deleted in this commit. Extends `D-2026-08-28-a-protocol-is-prescriptive-and-a-record-is-not`
without amending it.

## Context

`D-2026-08-28` built the prescriptive tier — what to run, as a revisable document — beside the
descriptive one it deliberately shares nothing with. Neither joined them. A design reached
`DesignStatus.executed` and **nothing attached the outcome**.

Three things were paying for that:

- `skills/hte-campaign-design` closes with *"the arms that survive become the observations
  `suggest_next_experiment` fits a surrogate to"*. That round trip was a person retyping a table,
  because `experiment_arms_from_campaign` goes campaign → design and nothing went back.
- The `DEFERRED.md` row on mining the agent-to-human protocol diff calls that diff *"the
  highest-quality supervision this system can collect about its own suggestions, and it is
  currently written and never read"*. It has no corpus, and could not get one: nothing recorded
  which designs had ever been **run**, let alone how they turned out.
- A plate run outside this system — the ordinary case, since the run sheet is a CSV somebody
  carries to a lab — had no way back in at all.

## Decision

**The join is its own table, keyed by `(design_id, revision, arm_id)`.**

That key is the decision, and it is what makes an externally-run plate attachable: a chemist who
ran the run sheet in another lab has a design id and an arm id and nothing else, so the table asks
for nothing else. `reaction_id` is **optional**, linking the outcome to its ELN transcription when
one exists and staying NULL when the run lives on paper.

**The revision is part of the key rather than the head.** A plate is run from the revision a
chemist printed; a later edit that drops a factor level must not silently re-point last week's
numbers at arms that no longer mean the same thing.

### The two cheaper answers, and why both are wrong

- **A `design_id` column on `reaction_records`.** Wrong twice. That table is fed by `ingest/eln`,
  whose exports carry no design id, so the column would be NULL on every ingested row and populated
  only by hand — a column that is almost always empty is one no query can rely on. And
  `D-2026-08-28` reuses *none* of that tier's shapes on purpose, because a record of what was done
  and an instruction to do it are different facts.
- **Outcomes folded into the design document, as a revision.** Wrong for the mirror reason. A
  revision is what a human *changed*, append-only precisely so the correction survives, and an
  outcome is not a correction. Folding them in would make "what did the chemist edit" unanswerable
  from the table that exists to answer it.

### Append-only, and the grant says so

Neither backend offers an update or a delete, and the application role is granted `INSERT` and
nothing else on the table. **A re-measured well is a second observation rather than a correction of
the first**: an assay repeated a week later on a degraded sample is data about the sample, and
overwriting would delete the evidence that the two disagree. `summarise` surfaces those
disagreements rather than resolving them; `latest_by_arm` takes the newest for callers that want
one number, which is most of them.

### Three refusals and one omission, all about the same failure

- **An outcome naming an arm the revision lacks is refused**, in Python rather than SQL: the arms
  live inside a JSONB document, so a foreign key that cannot see them would be a control whose
  condition never occurs. The refusal names the arms that do exist, because on a plate `A1` and
  `A11` are both plausible and the failure is otherwise silent — the outcome stores, counts toward
  nothing, and leaves the arm it was meant for looking unrun.
- **Unmeasured arms are named rather than counted.** `analytical.evaluate`'s argument about
  `not_measured`, one tier over: a summary of only what landed makes a half-run plate look
  finished.
- **An unmeasured arm is omitted from a campaign's observations, never defaulted.** A missing well
  is not a zero. Handing a surrogate a fabricated zero would make the fit confidently wrong in the
  direction of that arm's conditions, with nothing downstream able to see why.

## Consequences

**`suggest_next_experiment` can be fed from a plate this system designed**, which is the loop's
payoff: `read_plate_results` with an outcome named returns each measured arm's factor levels beside
its value.

**The supervision corpus becomes collectable.** A design with attached outcomes is a design that
was run, so the deferred protocol-diff miner now has something to count. That row still stands —
what it waits on is a deployment with real revisions, and it owes a count first.

**Prefix.** `attach_plate_results` costs 606 tokens and `read_plate_results` 257, and
`CEILINGS["__default__"]` rises to 73,100 to carry them. Unlike the other two raises on this
branch, both tools work in every deployment — they touch only core's design store — so the prefix
buys something every turn can use. The branch total is 2,500 tokens of thread allowance, and the
ceiling's own entry says a fourth raise should be refused.

**Not done here**: nothing yet *writes* an outcome from an ELN ingest, so `reaction_id` is filled
only when a caller supplies it. Joining a transcription to the design it was run from automatically
would need the ELN export to carry the design id, which is a question for the source seam rather
than this table.

## Revisit when

A deployment attaches outcomes to designs it did not create — an instrument integration, or an ELN
export that learned to carry a design id. That is the point at which `author_kind` stops being the
useful distinction and provenance needs a source column, and the file that would show it is
`experiment_arm_results` filling with rows whose `author` is one service account.
