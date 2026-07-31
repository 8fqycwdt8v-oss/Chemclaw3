# D-2026-07-31-adr-ids-that-cannot-collide — ADR ids that cannot collide

**Status:** accepted · **Supersedes the allocation half of:** D-147

## Context

`CLAUDE.md` has carried this sentence since D-147:

> If collisions somehow continue, the remaining fix is to abandon the global sequence for
> date-plus-slug ids, which cannot collide at all. That costs every existing citation, so it is a
> deliberate convention change — raise it, don't drift into it.

They continued. On one day, on one branch:

| | |
|---|---|
| ADRs written | 4 |
| times those ADRs were renumbered | 3 |
| numbers taken by other sessions mid-flight | D-156, D-157, D-158, D-162, D-163 |
| a second branch, same day | renumbered three times (`main`'s own log) |

Every collision was on a number **nobody had merged**. Not one was on a merged ADR, and that is the
observation the fix turns on.

D-147 diagnosed the original problem correctly — concurrent branches appending to one
`DECISIONS.md`, each picking "highest I can see, plus one" against a base that cannot see the
others — and fixed the half that was fixable by structure. One file per ADR means two claims to a
number collide on a *filename*, which git reports loudly instead of burying inside ninety lines of
prose. That worked. Detection is not the problem any more.

Allocation is, and it is not fixable by procedure, because the procedure's first step is a read that
is stale the instant another session pushes. `CLAUDE.md` asked an author to enumerate against
`origin/main` and reserve in their first commit; both were followed here, and the number was taken
anyway — twice — because the confirmed cause is that **many sessions run simultaneously**. No
amount of care makes a stale read fresh.

The renumber itself is not free. Each one is a `git mv`, a heading edit, a ledger move, and a sweep
for every citation — mechanical work with a real chance of missing one, performed under a merge
conflict, which is exactly when attention is scarcest.

## Decision

**New ADRs are named `D-YYYY-MM-DD-<slug>.md`.** Today's date, and a slug naming the decision.
Nothing to enumerate, nothing to reserve, nothing to coordinate.

**The id is the whole stem, not the date.** Two ADRs on one day is routine here — it is what the
table above describes — so `D-2026-07-31` alone would reproduce precisely the failure being
replaced: one id, two decisions. With the slug included, collision needs the same date *and* the
same slug, and even that surfaces as an add/add conflict on a filename rather than as a duplicate
nobody notices. The cost is that citations get longer, and that is accepted knowingly.

**The `D-NNN` sequence is frozen, not migrated.** This is where the decision departs from what
`CLAUDE.md` anticipated. That sentence assumed the change "costs every existing citation" — a full
rename of every ADR and rewrite of every reference. Measured on the day this was decided: ~167 ADR
files and ~971 citations across ~475 files — and it buys nothing: a merged ADR has never collided
and never can, because only unallocated numbers are contended and after this there are none. So the numbered ids stay exactly as
they are, every citation keeps resolving, and no in-flight branch conflicts with a rename it did not
ask for.

Both forms live in one ledger, numbered first and then dated. `tests/test_decision_log.py` knows
both shapes; `_sort_key` defines record order and the ledger is asserted against it.

**`RESERVED` rows become legacy.** They are kept, because three were in flight (D-159/160/161) when
this landed and a convention change must not strand other sessions' work. Nothing new needs one: a
dated id cannot be taken, so there is nothing to claim.

## Consequences

The renumber-on-merge rule, the reserve-in-your-first-commit ritual, the enumerate-against-main
incantation and the "branch merging second renumbers" tie-breaker all disappear. `CLAUDE.md`'s
section drops from ~64 lines to ~20, and most of what remains is history rather than procedure.

The ordering split is load-bearing in a way that is easy to get wrong, and worth recording because
the first version of its test did not catch it. Sorting stems as plain strings gives the correct
answer for every id in the record *today* — `D-001`…`D-167` are zero-padded, so lexicographic order
is numeric order, and they all begin `D-0`/`D-1`, which precedes `D-2025-…`. It becomes wrong at
`D-300`, where `"D-900-…" > "D-2025-…"` because `'9' > '2'`. A test built from `D-009`/`D-010`
passed against a deliberately flattened sort key; one built from `D-900` fails. The test now uses
`D-900` and says why.

Three mutations were run against this change — making the id the date rather than the stem, making
the filename pattern permissive, and flattening the sort key — because a scheme change is exactly
when a validator quietly stops validating, and because three tests earlier in this same body of work
turned out to pass against their own fix. Two of the three mutations failed the suite immediately;
the third is the sort-key case above, and it is the reason that test now looks the way it does.

What this does not address: nothing prevents two sessions from writing ADRs that *contradict* each
other, which is a review problem and always was. This only guarantees that two decisions never wear
one name.
