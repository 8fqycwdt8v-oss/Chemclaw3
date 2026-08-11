# D-2026-08-10-a-list-of-ids-is-not-a-conversation-list — A list of ids is not a conversation list, so the service names and orders its own sessions

**Status:** accepted · **Date:** 2026-08-10

## Context

`GET /sessions` returned `SessionSummary(session_id, created_at)`, and a sidebar cannot be built
from that. The companion UI ([`Chemclaw3_ui`](https://github.com/8fqycwdt8v-oss/Chemclaw3_ui))
proved it by working around it: every conversation restored from another device got the same
placeholder name, because there was nothing to name it with, and the list was ordered by when each
session was *minted*. Ten restored conversations were ten identical rows, sorted so the one most
likely to be wanted — an old one recently returned to — sat at the bottom.

Two facts were missing, and one class of row should not have been there at all:

|                            | before                                | after                                   |
| -------------------------- | ------------------------------------- | --------------------------------------- |
| name                       | absent                                | `title`, from the message that opened it |
| recency                    | `created_at`, i.e. when it was minted | `updated_at`, the last stored message   |
| warmed-but-unused sessions | listed                                | not listed                              |

The third is the front door's own doing. The UI creates the backend session on the first keystroke
so the first message costs one round-trip instead of two, so every abandoned draft leaves an
ownership row behind and `GET /sessions` listed it. **A client cannot filter those out from
outside**: a session nobody spoke in and a session whose transcript failed to load are both an empty
array. That is what made this the service's problem rather than the sidebar's.

This closes the backend half of issues 4 and 7 in the companion repo's `ISSUES.md`. The design and a
verified patch were written up there (`BACKEND-SESSION-LISTING.md`) by a session that could read this
repo and not write to it; this ADR is the decision as recorded *here*, where the code is.

## Decision

**1. The title is a column, not an expression over the stored message.** The tempting version
extracts the first user message's text from `session_messages.message`. `infra/sql/008_sessions.sql`
is explicit that the store does not interpret that JSONB — *"a MAF message-shape change is a value
change, not a schema change"* — so a `message->'contents'` expression would quietly convert every
future MAF shape change into a broken conversation list. The turn route already holds the user's
message as a plain string when it accepts a turn, so it writes the title from there and nothing
parses anything (`api/schemas.py::session_title`, collapsed and bounded at 120 characters, not
summarised). Nullable, for the reason `owner` and `profile` are: a session that has never had a turn
genuinely has no title. This follows migration 021's precedent for `profile` exactly — the row is
already "the facts about a session that must survive the LRU", and a name is one of them.

**2. Last activity is derived, not mirrored.** `max(session_messages.created_at)` per session, not
an `updated_at` column on `session_owners`. The turn that would have to maintain a mirror already
writes the row the derivation reads, and a second write per turn is a second thing that can fall out
of step with the first. Migration 043 adds `session_messages (session_id, created_at DESC)` to make
deriving it cheap — neither existing index serves a per-session `max(created_at)`: 008's
`(session_id, id)` is ordered by insertion id, and 022's `(created_at, session_id)` leads with the
wrong column.

**3. The lateral join is also the filter.** `max()` with no `GROUP BY` always returns a row — NULL
when there is nothing to aggregate — so a `JOIN LATERAL … ON m.updated_at IS NOT NULL` drops
precisely the sessions nobody ever spoke in. One query answers "what was the last activity" and "was
there any", which is **why this needs no cleanup job for warmed sessions**.

**4. `set_title_if_absent` is one conditional `UPDATE`, and the guard is what makes it safe.** The
route calls it on *every* turn — it has no cheap way to know which one is first — so `WHERE title IS
NULL` in the statement is what stops a sidebar entry renaming itself on every message, which is the
one thing a navigation label must not do. One indexed no-op write on the primary key, not a
read-then-write: the second shape costs two round-trips to discover it has nothing to do and can
lose a race between them. It is called after the turn claim, so a rejected double-submit does not
write, and before the stream, so a turn that fails mid-answer still leaves the conversation named.

**5. The grant widens, and says so where it widens.** `infra/sql/grants/app_privileges.sql` said, in
a comment, *"no update: `session_owners` upserts with `DO NOTHING`"* — true until decision 4 made it
false, and under a split-principal deployment the title write would have raised
`InsufficientPrivilege` **in production**. The grant is necessarily wider than the write, because
SQL has no column-level "only while null" privilege; the comment now says that rather than claiming a
narrowness the role does not have.

## Consequences

- `SessionSummary` gains a **required** `updated_at` and an optional `title`. A client written
  against the old shape keeps working (both are additions); a client that wants them gets a sidebar
  it can render without inventing placeholders.
- Sessions with no messages disappear from the listing. They are still owned, still authorized
  against, and still resumable by id — they are only not *listed*, because a created-but-unused
  session is not a conversation.
- `title` is NULL for every session whose first turn predates this change. Those rows are still
  listed; hiding a conversation because the service cannot name it would lose history to a schema
  change.
- Two suite failures were the change telling the truth about itself, and both are fixed here rather
  than worked around: `tests/test_database_privileges.py` on the new `UPDATE` (decision 5), and
  `tests/test_schema_inventory.py` requiring `043` in the Migration cell of **both** tables the
  migration touches — `session_owners` for the column and `session_messages` for the index.
