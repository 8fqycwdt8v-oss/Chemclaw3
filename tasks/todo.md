# Post-merge fix round — the adversarial review of `18fad815`

Eight findings from a fresh-context review of the merged "tool count in prose" commit. The first
two are the defect the guards in that commit were written to prevent: **a guard satisfied by being
broken**. Worked in the review's order.

## Plan

- [ ] **1 — the prose guard is satisfied by being broken.** `_CALC_SERVER_PACKAGE` is an unanchored
  path string and nothing asserts it resolves; renaming the package empties half the scan and the
  only assertion is `assert not offenders`. Assert the basis the way the neighbouring jobs rule
  does: at least one tracked file was actually *read* under the package.
- [ ] **2 — the readiness guard is defeated by a reword.** `assert claims` needs one claim to parse
  and two parse at HEAD, so rewording either leaves the other satisfying the anchor. Derive the
  *subjects* instead: every backticked token in the record that resolves to a real `settings` field
  must be claimed or argued.
- [ ] **3 — the ADR's generalisation is half wrong.** "What moved was its default" is true of the
  spend row and false of the fingerprint row, whose *implementation* changed. New ADR (a merged one
  is never edited) saying so and saying what the second class would need.
- [ ] **4 — the `#:` comment is wrong about its own regex.** `(?:all\s+)?` is optional, so a subset
  count matches. Measure whether making it mandatory keeps the original defect caught; correct the
  comment to what the pattern does.
- [ ] **5 — a dead assertion and a bare `AttributeError`.** `matched = [name]` unconditionally.
- [ ] **6 — "three shapes / three rows" is two at HEAD.** Delete the number from the `#:` comment,
  the test docstring and (via the new ADR) the record of it.
- [ ] **7 — a number *is* reasserted inside the guard's own scope.** `tools.py` says "over
  seventeen", the test docstring says "seventeen" twice, the manifest header says "seventeen
  primitives" about a surface in another repository. Delete, do not restate.
- [ ] **8 — the guard's docstring dodges its own pattern silently, and the exemption list is
  asymmetric.** Make the exemption explicit and argued; decide the `tasks/` vs `BACKLOG.md` axis
  deliberately and write down which axis produces it.

## Method

Every finding re-verified against HEAD before it is fixed, and every fix mutated and watched to
fail before its commit message is written, with `git diff --numstat` confirming the line changed.

## Review

(filled in as each finding lands)
