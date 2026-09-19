# Fix round, 2026-09-18 — the complement of `d6d828ba`

**Why this file and not `tasks/todo.md`.** The subject commit's PR body claimed a plan and review
under `tasks/todo.md` and the merged tree carries none of it: `git diff 44e7c0e1 d6d828ba -- tasks/`
is empty, because that file is a shared scratch file and this branch's copy lost the merge to
main's. Nothing depended on it, so nothing broke — the defect was the claim. A per-round filename
cannot lose that merge, and `tasks/` already uses the convention
(`review-2026-08-12-*.md`, `paperclip-review-round-2026-09-15.md`).

## The subject

A fresh-context adversarial review of `d6d828ba` (PR #394). All ten of that commit's claim groups
reproduced and both merge resolutions were proven lossless; none of that is re-litigated here. What
it found is the complement:

> The commit watched each guard FAIL on the defect it was written for. It never watched the guard
> PASS over other mutations of the same property.

Every item below is an instance, and the method is the point: for each one, reproduce against HEAD
first, then drive the original mutation **and at least two other mutations of the same property**,
with `git diff --numstat` after every mutation and every fix.

## Plan

- [x] **F2** readiness guard defeated by a reword that drops the backticks. Population becomes a
      union of literal field names and resolved backticked tokens; both sides compared as resolved
      fields. Measure the false-red cost first.
- [x] **F11** "Both shapes now go through one `_fields_named`" was false — hoist the resolution
      above the branch.
- [x] **F3** `assert scanned_in_package` is an *any* basis. Derive the package from the module that
      defines the surface (`find_spec`), leaving the assertion to guard the residue.
- [x] **F4** the `_CALC_BUNDLE` half has no basis at all. Derive the bundle name from its manifest,
      admit a modifier run, and assert some live file outside the package still names it.
- [x] **F5** a full stop defeats the live half. Pair at paragraph scope; measure the cost.
- [x] **F6** `_NAMES_WHAT_IT_FORBIDS` exempts the whole module. Exempt the quoted phrases instead.
- [x] **F12** one adjective evades the pattern. Widen or correct the comment — decide with an
      argument and a measurement.
- [x] **F1 / F1b** the new ADR states a default the tree does not have, and the same figure is live
      in the ledger. New ADR; the durable form is "a non-zero default, pinned by `<file>::<test>`";
      the ledger row is an index, not a merged record, so it is corrected in place.
- [x] **F10** `remote.py:502` names twelve `run_cached_*` wrappers and zero exist.
- [x] **F9** record where the scope stops and why the bundle's own manifest is outside it.
- [x] **F7 / F8** judgement calls, each with a written reason.
- [x] **F-RED** `test_a_build_that_cannot_meet_its_budget_costs_the_query_a_fraction_of_that_budget`
      fails serially and alone at HEAD. Root-cause it; do not loosen the threshold.
- [x] Reconcile `docs/planning/BACKLOG.md`'s open row for the sibling shape.
- [x] `tasks/lessons.md` — the organising insight as a rule for the next session.
- [x] One ADR for the round, one for the figure correction, ledger rows for both.

## Review

**What was reproduced.** All eight code/prose findings reproduced against HEAD before anything was
changed, each to the exact outcome the review reported. Nothing was rejected. `F-RED` reproduced and
turned out not to be a performance regression at all.

**What the round changed in kind.** Six of the seven guard fixes replaced an *assertion* with a
*derivation* or a *measurement*:

- the scope of the prose guard's package half is now resolved from the module that defines the
  surface, not spelled;
- its bundle predicate is built from the name that bundle's own manifest declares;
- the readiness guard's subject population is derived from the settings object rather than from a
  markup convention;
- three widenings (paragraph pairing, the bundle modifier run, the adjective run) were each
  measured for false positives before being taken, and each measured zero;
- the molfp test stopped asserting a wall-clock ratio and now asserts records parsed, against a
  budget calibrated from a full build measured in the same run.

**What is deliberately not closed, and is written where a reader finds it.** A glob subject in the
readiness record whose backticks are dropped is still invisible — `retention_*_days` is no field
name for a literal scan to find. The bundle half of the prose scope has an *any* basis and says so,
because the files that discuss a bundle from outside are not a derivable set. And the prose scope
stops at the surface package rather than the whole bundle, measured: widening it catches four
paragraphs of which two count something else entirely.

**A finding about the method itself.** The F6 fix red this round's own first draft — a new `#:`
comment wrapped a quoted count across a line so the closing quote was not adjacent, and the guard
reported it as a live count. That is the only evidence in this round that any of these guards works
on an author who is not trying to defeat it.
