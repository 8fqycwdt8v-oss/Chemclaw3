# D-2026-09-14-a-measurement-transcribed-into-a-record-is-a-claim-about-an-afternoon — the numbers wave 27/28 recorded that do not reproduce

**Status**: accepted. Supersedes figures — and only figures — in
`D-2026-09-14-a-lowering-that-loses-a-merge-is-a-raising` and
`D-2026-09-14-a-tripwire-over-two-named-modules-covers-the-modules-it-names`. Neither ADR's
argument or decision is touched; both were re-verified while the numbers were re-measured.

## Context

A fresh-context adversarial review of the merged waves 27/28 re-measured every figure those commits
wrote into a record. The code came through sound — every mechanism the reviewer mutated failed the
test it was supposed to fail. What did not come through is a set of digits, each written by the
session that had just measured the thing it was describing, and each wrong by the time it was
merged.

That is `D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` again, and it is worth recording
once more only because of *where* these landed: not in a docstring somebody edits next week, but in
merged ADRs, which are never edited, and in the ledger row a reader hits first.

## What did not reproduce

**The ratchet's headroom.** `D-2026-09-14-a-lowering-that-loses-a-merge-is-a-raising` says the
headroom is *"**740** now (67,200 against 66,460, measured here)"*. Measured at the reviewed commit,
twice, identical:

```
default prefix: 66545  ceiling: 67200  headroom: 655
```

**The cost of the restoration.** The same ADR says the `get_durable_job_status` rationale put back
measures *"**+212** tokens (66,460 → 66,672)"*. Driven here — the exact paragraph
`D-2026-09-14-a-docstring-is-a-prompt-and-a-comment-is-not` removed, restored verbatim, `git diff
--numstat` reading `8 3` — the prefix goes 66,545 → **66,649**, a rise of **104**.

+212 was also arithmetically unavailable before anyone ran it. That paragraph is measured at 184
tokens by the ADR that removed it, and the restoration *replaces* the three-line summary written in
its place; a replacement cannot cost more than the thing replaced.

## What is unaffected, and was re-verified rather than assumed

**The conclusion stands in full.** The claim it withdrew — that "the 309 tokens cannot come back
without failing the lowered ceiling" — is still false, by a wider margin than the ADR argued: the
restoration passes the ceiling with room to spare, and the control that actually fires is
`tests/test_prose_contract.py::test_no_tool_description_tells_the_model_about_a_tier_that_is_gone`,
driven red on its own at the restored tree. A ceiling holds aggregate drift; a paragraph is held by
a test that reads the paragraph. Nothing in that argument needed any of the three digits.

## The same wave, one record over: the calc seam's tripwire

`D-2026-09-14-a-tripwire-over-two-named-modules-covers-the-modules-it-names` widened
`tests/test_sibling_manifest_agreement.py`'s calc-seam check from a hand-kept two-module list to a
derivation, and got three figures around it wrong. Measured off `_hardcoded_calls()` and the fleet's
own `servers/calc/tool-surface.json`:

- The old basis is **13** call sites, which the ADR's title, its body and that file's own paragraph
  all say — while `_callers()`'s docstring, twelve lines below, said *"16 call sites"*. Corrected in
  place, because a test file is not a merged record.
- Its `## Consequences` says *"the **10** fleet `calc` tools nothing here calls stay uncovered"*.
  That is the figure from **before** its own change: the widened seam names 18 of the fleet's 20, so
  what stays uncovered is **two** — `optimize_geometry` and `predict_logd`. The consequence itself
  is unchanged and still right: a tool nobody calls cannot be called wrongly.
- Its list of the nine names the newly covered half carries includes **`predict_logd`**, which is in
  neither newly covered module and is one of the two the seam never names at all.

The ledger row is corrected the same way as the one above: by naming the two uncovered tools rather
than by restating a count, since a count over another repository's surface goes stale on somebody
else's merge and the two names do not.

## Decision

- **The figures named above are superseded here**, and the arguments they sat inside are not:
  neither the ceiling ADR's conclusion nor the tripwire ADR's decision turns on any of them.
- **The ledger row is corrected by deletion rather than by restatement.** `docs/decisions/README.md`
  is a live index and may still change, so it stops carrying a headroom and a delta at all. The
  ratchet's live number is whatever `tests/test_context_floor.py` measures against
  `CEILINGS["__default__"]`, which is the rule `CLAUDE.md` already states about this exact number
  and which the row was quietly outside.
- **The ADR itself is not edited**, because it is merged. A reader who opens it now arrives through
  a ledger row that names this record.

## Consequences

- No number here is offered as current. The two that are stated — 66,545 and the +104 — are stated
  as what a named commit measured, which is the only thing a transcribed measurement can be.
- The general rule is unchanged and is not a new one: a figure worth relying on lives in a test that
  reddens when it goes stale. This record exists because three did not.

## What keeps it true

- `tests/test_context_floor.py::test_the_static_prefix_stays_under_its_ceiling` — the live prefix
  against the live ceiling, the only place either number is current.
- `tests/test_prose_contract.py::test_no_tool_description_tells_the_model_about_a_tier_that_is_gone`
  — the control that the superseded bullet should have named, and that this review drove red again.
- `tests/test_sibling_manifest_agreement.py::test_the_calc_seam_calls_only_tools_the_fleet_records_serving`
  — the seam's live coverage, derived from who imports a dispatcher rather than from a list, which
  is why the corrected figures above are readable off the tree at all.
- `tests/test_decision_log.py::test_the_index_lists_exactly_the_decisions_on_disk` — this record
  is in the ledger beside the one it supersedes, so the correction is reachable from the row that
  carried the wrong number.
