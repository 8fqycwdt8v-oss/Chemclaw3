# Post-merge fix round — the adversarial review of `18fad815`

Eight findings from a fresh-context review of the merged "tool count in prose" commit. The first
two are the defect the guards in that commit were written to prevent: **a guard satisfied by being
broken**. Worked in the review's order.

## Plan

- [x] **1 — the prose guard is satisfied by being broken.** `_CALC_SERVER_PACKAGE` is an unanchored
  path string and nothing asserts it resolves; renaming the package empties half the scan and the
  only assertion is `assert not offenders`. Assert the basis the way the neighbouring jobs rule
  does: at least one tracked file was actually *read* under the package.
- [x] **2 — the readiness guard is defeated by a reword.** `assert claims` needs one claim to parse
  and two parse at HEAD, so rewording either leaves the other satisfying the anchor. Derive the
  *subjects* instead: every backticked token in the record that resolves to a real `settings` field
  must be claimed or argued.
- [x] **3 — the ADR's generalisation is half wrong.** "What moved was its default" is true of the
  spend row and false of the fingerprint row, whose *implementation* changed. New ADR (a merged one
  is never edited) saying so and saying what the second class would need.
- [x] **4 — the `#:` comment is wrong about its own regex.** `(?:all\s+)?` is optional, so a subset
  count matches. Measure whether making it mandatory keeps the original defect caught; correct the
  comment to what the pattern does.
- [x] **5 — a dead assertion and a bare `AttributeError`.** `matched = [name]` unconditionally.
- [x] **6 — "three shapes / three rows" is two at HEAD.** Delete the number from the `#:` comment,
  the test docstring and (via the new ADR) the record of it.
- [x] **7 — a number *is* reasserted inside the guard's own scope.** `tools.py` says "over
  seventeen", the test docstring says "seventeen" twice, the manifest header says "seventeen
  primitives" about a surface in another repository. Delete, do not restate.
- [x] **8 — the guard's docstring dodges its own pattern silently, and the exemption list is
  asymmetric.** Make the exemption explicit and argued; decide the `tasks/` vs `BACKLOG.md` axis
  deliberately and write down which axis produces it.

## Method

Every finding re-verified against HEAD before it is fixed, and every fix mutated and watched to
fail before its commit message is written, with `git diff --numstat` confirming the line changed.

## Review

All eight reproduced against HEAD before being fixed; none was rejected.

**1 and 2 were the same defect** — a guard satisfied by being broken — and both are now anchored on
their *basis* rather than their conclusion, which is the shape the neighbouring jobs rule already
used. The prose guard asserts that at least one tracked file was really read under the package it
derives half its scope from; the readiness guard derives the *subjects* it must cover from the
record itself, so a claim reworded out of the parsed shape leaves its subject named and unclaimed.
Both original mutations now fail with a message naming what moved.

**3 was an argument defect** and is a new ADR, since a merged one is never edited: the two stale
rows were two classes, a moved default (resolvable) and a changed implementation (prose about a
function body), and the guard covers the first. What the second would need is written down and
costed rather than built.

**4 was measured rather than argued.** Making the whole-surface quantifier mandatory loses the
module docstring that actually went stale, so the pattern stays broad and the comment says what it
really does.

**6 and 7 were the no-number rule broken inside the commit enforcing it**, and both are fixed by
deleting the figure, not by restating it — including one that counted a surface served out of a
repository this tree cannot watch.

**8's asymmetry was decided rather than smoothed.** The axis is append-only dated record against
live description: `tasks/lessons.md` stays exempt because every entry is dated even though CLAUDE.md
calls the file live, and `BACKLOG.md`/`runbook.md` stay unexempt because both are read as current.
Written where the next editor reads it, along with the exemption the guard's own docstring had been
relying on by accident.
