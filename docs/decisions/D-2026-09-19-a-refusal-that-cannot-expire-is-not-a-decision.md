# D-2026-09-19-a-refusal-that-cannot-expire-is-not-a-decision — a decline carries the condition that reopens it, a defect fix is not an ADR, and a dead vocabulary is marked by a test rather than by a roster

**Status:** accepted · **Date:** 2026-09-19 · **Builds on:**
D-2026-07-31-adr-ids-that-cannot-collide (one file per ADR, ids that cannot collide),
D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks (the "read this as"
marker this generalises), D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit (the same argument
applied to figures rather than to premises), D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose

## Context

The maintainer's report was that the record increasingly *blocks* feature work: an ADR says not to
do something, the reason it gives is no longer true, and the ADR is merged so nobody may edit it.
Four fresh-context audits — the declining ADRs, the planning registers, `CLAUDE.md`, and the sibling
fleet — were run to find out whether that is what is happening. It is, and not where it was expected.

**Three of the four suspects are healthy.** `DEFERRED.md` has 60 rows, 58 of 60 anchors resolve, one
dead premise, and seven rows that announce their own premise's death and re-argue the deferral on new
grounds — the file working. `BACKLOG.md` has 46 rows, 78% of them under five days old, and **zero**
rows resting on a deleted subsystem. `Chemclaw3-mcp` has 74 ADRs spanning seven days and **no** stale
constraint at all; all eight of its prohibitions are live and six are held by a test that agrees with
the prose token-for-token.

That last one is the control, and it is what rules out the obvious explanation. Same conventions,
same rule density, same authors, one-tenth the corpus age — no rot. **Rot is a function of corpus
age and volume, not of the practice of writing rules down.**

## What was measured

Over `docs/decisions/` (680 files, 40 active days, ~13 ADRs a day):

- **71 ADRs decline a class of future work. 4 state a condition for revisiting.** `DEFERRED.md` has
  required a "Trigger to revisit" column since it existed. An ADR that *declines* has never required
  one, and "rejected" is the stronger word. So a deferral expires and a refusal does not.
- **Every ADR that carries a status says `accepted`. None says superseded.** The 19 tests in
  `tests/test_decision_log.py` enforce id uniqueness, filename/heading agreement, index completeness
  and that cited tests resolve — referential integrity, never validity. The record can be
  well-formed and false at the same time, and nothing can tell.
- **231 of 680 ADRs are cited by no row of the "By topic" table**, whose cursor covered only ADRs
  newer than 2026-09-01. For a third of the record, nothing says whether the decision is current.
- **96 ADRs describe the deleted PR-gate as a live mechanism**; 53 the removed agent framework; 59
  the dropped GxP framing; 13 the deleted HPC tier. The cure already existed — the
  `## Where the record still says "GxP"` marker — and had been applied to exactly one of them.
  **That marker had itself gone stale**: it listed 59 files by hand where 64 matched, three of them
  written *after* the vocabulary was dropped, under a sentence calling them "a quarter of this
  corpus" when the corpus had tripled and the true share was a tenth.

Ten declining ADRs were checked against the tree and found to rest on an expired premise. Six of the
ten rest on one of two deletions — the PR-gate and the second LLM provider. The sharpest is not in
that six:

> **`D-092` stated its reopening condition, it was met in another repository, and nobody noticed —
> including the first draft of this ADR, which got the conclusion right off the wrong evidence.**
> It declined ML interatomic potentials and AiZynthFinder retrosynthesis under one condition:
> *"Revisit only if a deployment vendors the weight files into the container image at build time as
> an explicit, reviewed infrastructure decision… not as a quiet runtime fetch."*
>
> That is shipped, **twice**, in `Chemclaw3-mcp`: `servers/rxnpredict/Containerfile` and
> `servers/rxnlabel/Containerfile` fetch their checkpoints in a build stage, pinned to 40-hex commit
> SHAs, under an `MCP_EGRESS_ALLOW` allowlist **with the egress guard armed**, and the runtime stage
> sets `HF_HUB_OFFLINE=1` and re-arms the guard. Reviewed, explicit, build-time, not quiet.
>
> This ADR's first draft cited `D-135` and `data/vendored/` instead — and that is a *dataset
> retriever* whose one shipped corpus is 1.9 kB of first-party CSV and whose manifest says in its own
> `retrieved_from` field that *"no third-party corpus has been vendored yet"*. Right conclusion,
> wrong mechanism, wrong repository. **And for the retrosynthesis half the condition was the wrong
> one entirely**: `docs/planning/DEFERRED.md` moved that blocker on 2026-08-13 — *"its blocker is not
> the weights and not their licence, it is the dependency closure"* — four mutually exclusive pins
> against `rdkit`, `numpy`, `networkx` and `pandas`, which vendoring cannot fix.
>
> So the condition was met by a mechanism nobody was watching, in a repository nobody was watching
> it from, while the register had quietly rewritten half of what it was a condition *for*. A trigger
> is not a control. It makes a question askable and answers nothing.

And the cause of the volume, which is the cause of everything above: **60% of these files read as
defect reports and 14% weigh an alternative.** A bug fix is being minted as a permanent,
never-edited document stamped `accepted` whose title is a rule. That is where 680 files in 40 days
comes from, and a stale constraint is what it leaves behind.

`CLAUDE.md` had the same disease in miniature and is measured in
`D-2026-09-19-a-changelog-in-the-instruction-file-is-a-changelog-nobody-versioned`.

## Decision

**1. An ADR that declines a class of future work carries `Revisit when:`.** The condition that would
make it worth reopening, in the shape `DEFERRED.md` has always required.
`tests/test_declines_carry_a_trigger.py` holds it from its cursor forward, on the same ratchet shape
as `_TOPIC_CURSOR` — including the guard that stops the cursor moving ahead of the record and going
vacuously green. It is a bound on the trigger being *written*, not on anyone checking it; `D-092` is
why that distinction is stated rather than assumed, and why the rule asks for an executable trigger
where one is available.

**2. A defect fix is a commit and a test, not an ADR.** Write an ADR when a choice between options is
taken, or when something is declined. Write a defect in the commit message and the guard in a test.
This is the only one of the four with no mechanism behind it, deliberately: a test that could tell a
defect report from a decision would have to read the prose, and a heuristic over 680 files would be
turned off inside a week. It is a review rule, and saying so is the point.

**3. A dead vocabulary is marked once, in the index, and held by a test.**
`tests/test_dead_vocabulary.py` carries one entry per term — the patterns, and the ADR that killed
it — and asserts that the term has a marker section in `docs/decisions/README.md`, that the killing
ADR exists, and that **no ADR written on or after the killing date uses the term** unless it is
argued in an allowlist that may not outlive its files. Three sections now exist: GxP, the PR-gate,
and the second LLM provider.

**The roster of affected files does not live in the index.** That is the half of the GxP marker that
failed, and it failed the way this repository's own rule predicts: a hand-list is a measurement
transcribed into a document. `grep -rl` answers "which files" correctly on the day it is asked. What
a list cannot do and a test can is stop the *next* file.

**4. The arrears of unfiled ADRs are a bound that may only shrink.** `_TOPIC_CURSOR` stops a new
decision landing unfiled and says nothing about the 231 already there.
`test_the_arrears_of_unfiled_adrs_only_shrink` counts them, fails if the number rises, and fails
*also* if it falls far below the allowance without the allowance being lowered — a bound that
silently stops binding is the defect class this repository cites most.

## What this deliberately does not do

**No merged ADR is edited and none is deleted.** The ten with expired premises keep their text; what
changes is that the index says so, and that the three largest dead vocabularies are marked rather
than one. An older ADR is still correct about the moment it was written, which is the whole reason
it is kept.

**No constraint is relaxed on the strength of this decision.** The audits checked the live
prohibitions too and found almost all of them sound — no-egress, no DFT, the composite/primitive
boundary, no agent-written `SKILL.md`, `manifests-internal`, no `assert` in serving code,
`ModelCallLimitMiddleware`'s composition rule. What was broken is the record's ability to say when a
*no* stopped applying, not the `no`s.

**It does not reopen the ten.** Each is now visible as revisitable; whether to revisit any is its own
decision. `D-092` was checked first, and the answer is that it splits rather than reopens: its
MACE-OFF/MACE-MP half is refusable on a firmer ground than it ever stated (the *weights* carry a
non-commercial Academic Software Licence, while the code is MIT), its ANI-2x/AIMNet2 half is blocked
on **demand rather than on vendoring**, and its retrosynthesis half is governed by a live
`DEFERRED.md` row whose blocker is a dependency closure. That is a separate decision and gets a
separate ADR.

## Revisit when

- A term in `tests/test_dead_vocabulary.py` turns out to need per-file treatment rather than a rule —
  i.e. the allowlist grows past a handful, which would mean the term is not dead but narrowed.
- The decline regex in `tests/test_declines_carry_a_trigger.py` produces enough false positives that
  `_EXEMPT` becomes the normal path rather than the exception.
- Rule 2 fails to hold in review: if the ADR-per-day rate does not fall, a review rule was the wrong
  instrument and the mechanism question reopens.

## What keeps it true

- `tests/test_dead_vocabulary.py` — the three marker sections exist, name their killing ADR, and no
  ADR written after a term died uses it unargued.
- `tests/test_declines_carry_a_trigger.py` — a decline newer than the cursor carries `Revisit when:`.
- `tests/test_decision_log.py::test_the_arrears_of_unfiled_adrs_only_shrink` — the unfiled count is
  a ceiling, and one that cannot go vacuous.
- `tests/test_claude_md_figures.py` — no figure in `CLAUDE.md` resolves to nothing.
- `tests/test_prose_contract.py` — the ADR ids this decision cites all have files.
