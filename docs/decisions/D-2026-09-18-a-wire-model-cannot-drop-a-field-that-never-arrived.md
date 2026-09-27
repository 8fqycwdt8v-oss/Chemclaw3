# D-2026-09-18-a-wire-model-cannot-drop-a-field-that-never-arrived — the three fields `GET /check-ins` owes the card reading it

**Status:** accepted · **Date:** 2026-09-18 · Extends
`D-2026-09-15-the-requester-hears-nothing-until-it-is-too-late`, which built the sweep and the
route. Supersedes nothing: every predicate, bound and refusal that ADR argues for is unchanged.

## Context

`Chemclaw3_ui` built the surface that opens the check-in mailbox, and building it found the wire
model thinner than the shapes behind it. Three fields, each costing that page one thing it already
does in a sibling section: `kind` is what the pending inbox badges every row by off `GET /pending`;
`session_id` is the link both other inboxes on `/review` end in, where a check-in row ended
nowhere; and `truncated` is what lets a list say it may be short, which `PartialScan` already says
for plans.

Two records described the gap — a `BACKLOG.md` row here and `Chemclaw3_ui`'s `ISSUES.md` Issue 16 —
and **both described it wrongly in the same direction**. Each said `CheckInOut` *drops* the three,
and each named `_check_in` as where two of them are dropped. Measured on the commit before this
one, against a migrated database:

| field | on `pending_requests` | selected by `_BLOCKED` | on `BlockedRequest` | in the mailbox payload | on `CheckInOut` |
|---|---|---|---|---|---|
| `kind` | yes | yes | yes | yes | **no** |
| `session_id` | yes — read back as written | **no** | no | no | no |
| `truncated` | n/a — a page property | n/a | n/a (`CheckIn`) | **no** | no |

So exactly one of the three was dropped at the wire. `session_id` was a column the sweep's own
query did not select, and `truncated` was a `CheckIn` field `_tell` never wrote: its payload was
`{"requests": [...]}` and nothing else. **A reader cannot drop what never arrived**, and the
consequence of getting that backwards is a patch aimed one layer too high — three fields added to
`CheckInOut`, two of which would then have been read out of a payload that has no such key and
served as `""` and `false` for ever, with every assertion at the route green.

That is the same shape as the defect the sweep's own test file already records: two halves each
passing their own tests while the seam between them delivered nothing.

## Decision

All three are added, at the layer each is actually missing from.

**`kind`** is an additive field on `CheckInOut` alone. It was already in the payload.

**`session_id`** is added to `_BLOCKED`'s `SELECT`, to `BlockedRequest`, and to `CheckInOut`. It is
the requester's *own* conversation — the query is scoped to `requested_by` and the route claims only
the authenticated principal's mailbox — so it reaches nobody it does not already belong to. It is
empty for a wait nobody opened in a conversation (a BO plate run, a connector job), which the UI
renders as no link rather than as a broken one.

**`truncated`** is written by `_tell` beside `requests`, and stamped by `_check_in` onto every entry
the claimed row carried. A page property on a per-request model is a deliberate choice, not a
compromise: `read_check_ins` answers a flat, unbounded list flattened across claimed rows, so there
is no envelope to put it on without changing the response type — and a reader asking "is this list
complete" is looking at an entry.

**Restating rather than importing stands.** `CheckInOut` is still `BlockedRequest` written out a
second time, which is what made each of these three an explicit decision here instead of a worker
shape leaking to a client. This ADR is that decision being taken, once, for three fields.

Two fields are deliberately still not sent. **No timestamp**: a digest has none either, and the
card says "claimed" rather than "asked", which is the honest word for a mailbox whose read is the
consume. **No `requested_by`**: it is the caller, by construction.

## Consequences

Every new key is additive and defaulted on both sides, so a payload an older sweep wrote still
reads — which matters more here than elsewhere, because the claim is the consume and a
`ValidationError` at this route destroys the notice rather than deferring it.

A deployment mid-upgrade serves `kind` immediately (it was already in the payload) and serves
`session_id` and `truncated` from the first sweep the new worker runs. Until then both read empty,
which is what the leniency is for.

## What keeps it true

- `tests/test_check_in.py::test_a_blocked_question_carries_its_kind_and_the_conversation_that_raised_it`
  drives the real query on a migrated database and asserts both columns survive it. It fails with
  `AttributeError: 'BlockedRequest' object has no attribute 'session_id'` against the unfixed tree.
- `tests/test_check_in.py::test_the_wire_carries_what_the_card_badges_links_and_warns_by` asserts
  all three at the route, which is the only layer that decides what a client may see.
- `tests/test_check_in.py::test_a_payload_written_before_these_fields_is_still_read` writes exactly
  the payload the previous release wrote and asserts it is still served, with the three new fields
  empty rather than a failure.
- `tests/test_check_in.py::test_a_requester_served_short_is_told_so_by_the_sweep_rather_than_only_by_email`
  runs the real `CheckInWorkflow` against the real activities on the broker for a requester with
  more questions than one page carries, and reads the flag back out through `GET /check-ins`. It is
  the one that catches the seam, and the one the two backlog records' framing would have left
  uncovered.
