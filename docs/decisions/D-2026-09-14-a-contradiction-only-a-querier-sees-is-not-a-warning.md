# D-2026-09-14-a-contradiction-only-a-querier-sees-is-not-a-warning — the corpus disagreeing with itself reached the chemist who happened to ask, never the one watching the subject

## Status

Accepted.

## Context

`kg/conflicts.py` finds every disagreement in the corpus — declared (`contradicts`, `supersedes`)
and suspected (a heuristic over overlapping subjects with a confidence gap) — and
`retrieval.retrievers._conflict_index` flags every chunk of a disputed note at *retrieval* time.
That is real and it is one-sided: it warns the chemist who happens to run a query. The chemist who
subscribed to a standing query on that subject is told nothing, and the corpus starting to disagree
with itself on their subject is the one thing in a digest that changes what they should do next.

The audit's wave-4 item for this reads "a conflict sweep that tells somebody". Before building one,
the question is who *somebody* is, and the answer measured out of the schema is the finding:

**A note has no author.** `Note.created_by` is `Literal["human", "agent"]` and `Note.source` is the
ingest source that transcribed it. Neither names a person. So "tell the note's author" is not a
thing this system can do — the same shape as `audit_events.agent` being empty on every row it has
ever written (`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`) and as
`session_messages` having no actor column (`PLAN-2026-09-14-multiplayer-…`). Three subsystems, one
missing column.

## Decision

**The addressee is the subscriber, and the notice rides the digest.** `agent/subscriptions.py`
already records who asked to be told about what, `durable/digest.py` already delivers it with a
watermark, per-subscriber isolation, a durable mailbox and — since
`D-2026-09-14-a-declared-kind-with-no-producer-is-not-a-channel` — an out-of-the-building copy. A
conflict on somebody's standing query is exactly a digest item. So no new schedule, no new kind, no
new addressing scheme, and no note-author column: `DigestItem.disputed` is the subset of that
subscriber's new matches the corpus disputes.

**The sweep is the one `retrieval` already warms.** `conflict_index(knowledge_path, date.today())`
is cached behind the corpus fingerprint, so the digest pays a hit rather than a scan on a corpus
retrieval has read. `as_of` is today for the retrieval-time reason: a superseded note is already out
of the current-evidence sweep, and reporting it as disagreeing with its own replacement is noise
rather than news.

**A dispute is marked in place, with a count.** The reader's question is "what is new"; a dispute is
a property of an entry in that list, not a second list. The trailing count is the half a reader acts
on, by `kg/conflicts.py`'s own rule that a silent truncation reads as completeness — "2 of 9
disagree" is what makes the marks countable without re-reading.

**And it is news, so an agreeing corpus carries no line.** A notice appended unconditionally is a
warning every reader learns to skip, which is the same harm as not warning them. The first guard
written for that did not fire: it asserted the word "disputed" was absent while the appended line
says "disagree with something already in the graph", so the mutation that appends unconditionally
left it green. A guard against an extra line has to be about the line, not about a word somebody
chose — the body is now asserted as exactly the list.

**One renderer.** Three places built this list and two disagreeing would be two answers to one
question; `_digest_body` is the one, and a test scans the module for a second. The session mailbox
is not a third: it carries the two lists as structured fields, so a client renders them.

## Replay

`DigestItem` is an activity's *return*. A run opened on the previous release replays a recorded
result with no `disputed` key, so the field defaults to empty — which is the truth about that
payload rather than a default standing in for one, and the deprecated `deliver_digest_activity`
shim passes `()` explicitly for that reason. No command changes, so no new `workflow.patched`
beside the one this workflow already carries.

## What this does not do

It does not tell the author of the note being contradicted, because the graph does not record one.
Adding `Note.author` is a schema change plus a backfill question over every note already written,
and it is the same column three subsystems now want — so it is a decision to take once, across all
three, rather than three times in the corner each of them noticed it.

## Consequences

- A subscriber whose subject is contested now learns it on their next digest instead of on their
  next query.
- `collect_digests` reads the conflict index, which is a cache hit on a warm corpus and a full scan
  on a cold one — the same scan the first retrieval of the day already pays.

## What keeps it true

- `tests/test_digest.py::test_a_new_note_that_contradicts_an_existing_one_is_marked_in_the_digest`
  (driven over a real corpus with a real `contradicts` relation, not a stubbed index),
  `::test_an_undisputed_digest_says_nothing_about_disputes`,
  `::test_the_body_marks_a_dispute_in_place_and_says_how_many`,
  `::test_every_render_of_one_digest_says_the_same_thing`.
