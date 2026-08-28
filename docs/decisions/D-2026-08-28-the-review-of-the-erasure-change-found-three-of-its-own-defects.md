# D-2026-08-28-the-review-of-the-erasure-change-found-three-of-its-own-defects — what an adversarial pass over the memory-bounds change found, and the two claims it retracts

**Status:** accepted · **Date:** 2026-08-28 · Corrects two factual claims in
`D-2026-08-28-an-erasure-that-cannot-name-what-it-missed` and one in
`D-2026-08-28-the-budget-is-the-control-not-the-trigger`. Neither is superseded: their decisions
stand and their mechanisms are unchanged.

## Context

Those two ADRs merged as one pull request, green under `make lint type test` with Postgres up
(5,532 passed), and reviewed only by their author. Four adversarial passes were then run over the
merged diff — three of them against a live database with the real sweeps, plus this repository's own
review target. They found **three defects in that change**, two claims in its ADRs that are false,
and one section heading it had deleted.

This ADR records them, because the alternative is a merged decision whose ADR states two things
that are not so, and a merged ADR is never edited.

## The three defects, all now closed

**1. One malformed payload made erasure impossible for the entire deployment.**
`_RETAINED_IN_PAYLOAD`'s predicate calls `jsonb_array_elements(document -> 'publications')`, a
*partial* function: handed a JSON `null`, an object or a scalar it raises. The retained count runs
in the same transaction as every DELETE, so **one** row of an unreadable shape — in a table this
command does not even erase — turned every actor's erasure into `ErasureError: cannot extract
elements from a scalar`, permanently, with no operator workaround short of editing that row by
hand. Measured across five shapes; three of the five abort.

That is precisely the rule the same module already applies to the checkpointer tables — *erasure
must not become the one operation such a deployment cannot perform* — broken in the tier that is
only supposed to count. A `jsonb_typeof(...) = 'array'` guard short-circuits it, and the test is
parametrized over the shapes Postgres refuses, because "it does not raise on the one I thought of"
is what the first predicate could already claim.

**2. The leaver's own orphaned link spared the leaver's own blob.** The new anti-join asked whether
a link *outside* the leaver's sessions existed. `delete_session` deliberately leaves a link row
when its blob is shared — so a chemist who tidied up one of their own sessions and later asked to
be erased kept their untruncated tool output, while the report printed `tool_result_blobs: 0`,
which reads as "there were none". The comment justifying it said such a link "no longer resolves to
a person"; in that case it resolved to the leaver, and the erasure is what made it unattributable
afterwards.

The anti-join now goes through `session_owners`: a blob is spared only while some link belongs to a
session that still has an ownership row naming somebody who is not the leaver. An orphan spares
nothing — the session it names cannot be reopened by anyone — and the cascade takes the orphan link
with the blob. The two tests are a pair on purpose: one proves another *person* still spares the
blob, the other proves an orphan does not, and a fix satisfying only one of them is the defect in
the other direction.

**3. A section heading went with a moved row.** Removing the campaign row from `BACKLOG.md` took
`## 2 — Answers that are wrong without saying so` with it, silently re-filing three rows under
"Untrusted input reaching a privileged surface" and leaving the numbering 1, 3, 4, 5.
`tests/test_backlog_register.py` has no heading guard, so it shipped green.

## The claims retracted

**`an-erasure-that-cannot-name-what-it-missed` §5 says three tables read *no decision is on
record*.** Two did. `plan_approvals` read `**nothing bounds it** — consumed rows are marked, never
removed`, which is a stated reason rather than a blank. All three read *nothing bounds it*, which is
the accurate quotation and the one the register now carries.

**It also says `unwindowed_ownership_dependencies` is derived from `_SESSION_SCOPED_ROWS` "so it
cannot drift".** It is not: `_OWNERSHIP_DEPENDENCIES` is a second hand-written map, and nothing
joined them — the exact defect that ADR is about, two screens below the paragraph naming it. A test
now asserts the two maps name the same set.

That map was also wrong about `session_events`. It named
`CHEMCLAW_RETENTION_SESSION_EVENTS_DAYS` as what would unblock it, and `_PRUNABLE` prunes that table
only `WHERE consumed_at IS NOT NULL` — so the population that actually accumulates, the *unconsumed*
one, blocks its ownership row at **every** window. The entry is `None` now, which says "no window
empties this" rather than offering a knob that does not work.

**`the-budget-is-the-control-not-the-trigger` quotes 90,366 tokens** for the 20 × 60 kB fixture.
The fixture `tests/test_compaction.py` builds gives **90,090**; 90,366 came from a different one.
The mechanism and the magnitude are unaffected — the point is that the bound holds — but a number a
reader cannot reproduce is the thing this repository asks prose not to contain.

## What is filed rather than fixed

The compaction review found a larger gap that this change made *live* rather than created:
`effective_trigger` charges the request's ~30k prefix against the budget only when
`llm_context_window_tokens` is declared, and no deployment artifact declares it — so with the floor
off, a request measures ~135,700 estimated tokens against a configured 100,000, and
`_record_overrun` cannot see it because it compares the thread against the thread's own budget.
Both are in `D-2026-08-28-a-budget-in-the-wrong-unit-is-not-a-budget`'s code, and the candidate
fixes are not equivalent — one of them changes what `agent_context_token_budget` *means*. That is a
decision with an owner, not a review pass's to take, and it is a `BACKLOG.md` row with the
measurement in it.

## The rule worth keeping

Every defect above is in code that was green, reviewed against its own tests, and argued for in
prose. What found them was **running the thing adversarially against a live database** — five
payload shapes, a session deleted before an erasure, a mutation sweep over a default. The tests
written alongside the change all passed with every one of these defects present, because each test
was written from the same understanding that produced the defect.

So: a change that ships a new SQL predicate over a payload nobody validates should be parametrized
over the shapes that predicate cannot read, before it merges rather than after.
