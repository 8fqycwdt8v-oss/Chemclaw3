# D-2026-08-15-a-claim-is-a-mutex-not-a-line-edit — GitHub Issues own who is working on an item; `BACKLOG.md` stays the prioritized list

**Status:** accepted

## Context

`docs/planning/BACKLOG.md` is a flat, git-tracked Markdown file: a prioritized list of open items,
each with its own rationale. Nothing in it, or anywhere else in `docs/planning/`, records who is
currently working an item — `grep -i "in progress\|WIP\|currently working\|claimed by\|assignee"`
across `docs/planning/` returns one unrelated hit. A user asked whether GitHub Issues would help
track the backlog and the "who's on this right now" question better.

This repository already solved the adjacent problem once, for a different flat file. D-147 and
D-2026-07-31-adr-ids-that-cannot-collide diagnosed why ADR numbering kept colliding: "highest
number on `origin/main`, plus one" is a read that goes stale the instant another concurrent session
pushes, and no amount of care — enumerating against `origin/main`, reserving in the first commit —
makes a stale read fresh, because the repository runs many sessions at once (measured there: five
collisions in one day, all on numbers nobody had merged). The fix was to stop needing a shared read
at all — date-plus-slug ids that cannot collide — rather than to discipline the procedure further.

A backlog claim is the same shape. "Add my name to this row" is a read of the file's current state
followed by a write back to it; two sessions racing to claim the same item either produce a merge
conflict on the same line (best case — noticed) or, if they happen to touch different lines of the
same row, a silent double-claim that nothing detects until both show up with a PR for the same work.
Unlike ADR ids, this doesn't need a *naming* fix — a claim isn't an identity, it's a piece of mutable
state (who owns this, is it in progress, is it done) that changes hands over the item's lifetime, so
what it needs is a place that resolves a race atomically. A git-tracked Markdown file, edited by
whoever gets there first, is exactly the kind of shared mutable state that read-then-write races
happen to.

## Decision

**`BACKLOG.md` keeps its current role unchanged**: the prioritized list of open items, with their
rationale and context, exactly as it is written and reviewed today. Nothing about how it is
populated or reprioritized changes — that's a normal PR-reviewed edit, not a race, because it has no
notion of "mine."

**A GitHub Issue is opened only when someone actually starts an item** — not one per backlog row,
and not in advance. The issue body links to the `BACKLOG.md` row (or the ADR that produced it, e.g.
`docs/decisions/D-2026-08-14-the-coupling-is-the-cost-not-the-line-count.md`) rather than duplicating
its rationale, and the `BACKLOG.md` row gains a trailing `(issue #NNN)` marker so the two point at
each other. From that point the issue, not the row, owns:

- **The claim.** Assignment is a single atomic call against GitHub's API, not a local file edit —
  two sessions racing to claim the same item get one clean winner and one clear "already assigned"
  failure, instead of a merge conflict or a silent double-claim.
- **Status.** A label (`in-progress`) plus the linked PR, which GitHub closes automatically on
  merge — no second place to remember to update.

**Closure deletes the row, in the same commit that merges the PR** — the same rule `DEFERRED.md`
already states and `docs/planning/BACKLOG.md`'s own header implies but doesn't enforce: an item that
outlives its closure reads as live state. `DEFERRED.md`'s docstring names the failure mode directly —
appending a status note under a stale row instead of deleting it is what turned that file into nine
chronological sections describing each other, three of them false (D-154). The same discipline
applies here for the same reason.

**Not test-enforced, and that is a deliberate exception, stated rather than hidden.** Every other
piece of this repository's process state that a rule governs has a machine check next to it —
`test_decision_log.py` for the ADR ledger, `test_deferred_register.py` for `DEFERRED.md` — because
D-2026-08-08-a-rule-with-no-test-is-a-claim found that roughly a third of one review campaign's own
new defects were exactly this: a rule stated in prose and never checked. An Issue lives outside the
repository a CI job can read on an offline runner, so nothing here can assert that every
`(issue #NNN)` marker still resolves to an open issue, or that every open issue has a marker. This is
a convention enforced by habit and by the PR review that already reads `BACKLOG.md` changes, not by
a test — the honest alternative to pretending a test exists.

**Not a duplicate-source-of-truth problem, and here's why it's different from the cases that were
declined for exactly that reason.** KM-9 declined a Postgres RLS mirror of the knowledge graph
because a mirror adds a sync pipeline and a second source of truth for the *same* facts (D-004). This
splits two *different* facts across two places instead — `BACKLOG.md` owns priority and rationale,
the issue owns claim and status — so there is nothing for the two to disagree about beyond the
`(issue #NNN)` link staying resolvable, which is the one thing this decision doesn't get to enforce.
It's also unlike the declined LangSmith adoption
(D-2026-08-11-the-observability-gap-is-real-and-langsmith-is-not-its-shape): that was rejected because
its core value is prompt/response *content* held in a third-party service, which several merged
decisions forbid. GitHub is not a new third-party dependency here — this repository already relies on
it for the PRs every change ships through — and an issue under this convention carries no
prompt/response content, only backlog metadata.

## Consequences

- `CLAUDE.md`'s "Persistent knowledge" list gets one line pointing at this split, so a session
  starting cold knows to check an item's linked issue for who's on it, rather than assuming
  `BACKLOG.md` is the whole answer.
- Nothing here requires opening an issue for every row today — only `BACKLOG.md` rows that are
  actually about to be picked up. Existing rows get an issue the next time someone starts one, not in
  a bulk migration.
- The unenforced half is a real gap, not a rounding error: a session that forgets the `(issue #NNN)`
  marker, or forgets to delete a closed row, degrades this back to exactly the flat file it started
  as. If that turns out to happen often, the trigger to revisit is in
  `docs/planning/DEFERRED.md` under "gated on a scale not yet reached" — a lightweight lint (does
  every `(issue #NNN)` marker in `BACKLOG.md` resolve, via the GitHub API, at PR-review time in CI)
  would close the gap this ADR left open, and wasn't built now because there is not yet a single
  real issue to check it against.
