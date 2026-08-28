# D-2026-08-27-a-session-list-is-a-cursor-and-a-session-is-deletable — the two ends of the conversation list

## Status

Accepted. Closes the `BACKLOG.md` row "No session pagination and no per-session delete", whose
original claim — that a data-subject erasure request "has no route across the seven tables" — was
already false when it was written and had been corrected in place: `chemclaw.agent.leaver` erases
across twelve tables in one transaction, with per-table counts and a dry-run default, shipped as
`make user-erase`. What was actually missing is what this ADR builds.

## Context

`GET /sessions` answered at most `service_max_listed_sessions` rows, newest first, and said nothing
about there being more. That cap is not a detail a client can work around: session ids are minted
server-side and returned once, into the response that created them, so a conversation that falls off
the bottom of that list is unreachable from any client that has lost its local state — while its
transcript sits in the store being retained, listed by nobody, and deletable by nobody.

And there was no delete. `leaver.erase_actor` is the only removal path in the system and it is
*actor*-scoped by design: it answers "someone left", takes everything conversational that person
owns, and reports the attributable rows it deliberately keeps. A chemist who wants one conversation
gone had no route at all, and the nearest available one would have taken every other conversation
they have with it.

## Decision

### 1. The listing pages by a keyset cursor, and the ceiling becomes the page

`page_for_owner(owner, after=…)` resumes the listing strictly after the row a cursor names;
`service_max_listed_sessions` stays exactly what it was — the most rows one answer may carry — and
becomes the page size rather than the end of the list.

**Keyset, not `OFFSET`, because this list reorders itself while it is being read.** It is ordered by
*last activity*, so a chemist speaking in an old conversation moves it to the top: rows crossing the
page boundary upward push the rows below it down, and an offset page then re-serves a row the caller
already has while never showing the one it displaced. Comparing against the sort key instead names a
*position in the ordering* rather than a count of rows before it:
`(m.updated_at, o.session_id) < (%s, %s)`, compared row-wise, with the session id as the tiebreak
that makes the order strict and therefore "everything after this row" unambiguous. The resume arm is
self-disabling (`%s::timestamptz IS NULL`), the shape `kg/proposal_store._SELECT_MANY` established,
so the first page and a resumed one are the same statement rather than two that can drift —
`list_for_owner` is now `page_for_owner` from the top and holds no query of its own.

**The cursor's stability guarantee** is exactly that it is the sort key of a row and nothing else:
`(updated_at, session_id)`, base64url-encoded. It therefore keeps meaning the same thing when rows
are inserted, deleted, or reordered under the reader; it does not depend on the page size; and the
row it was minted from need not still exist. What it does *not* promise is a consistent snapshot —
a conversation that moves above a cursor the reader has already passed will not be shown again, and
that is the correct answer rather than a gap: the caller has seen it, at the top, one page earlier.

It is opaque so that no client learns to construct one — a constructed cursor breaks the day the
ordering gains a third component — and it is deliberately **not signed**. A cursor is not a
capability: every page is re-scoped by `owner IS NOT DISTINCT FROM`, so the worst a forged cursor can
do is move the forger around their own list. One that does not decode is a 422, never a 500 and never
a silent first page, because silently re-answering page one makes a client page forever.

**The cursor is returned in an `X-Next-Cursor` response header, and the body does not change.**
`GET /sessions` answers with a bare JSON array and the companion UI parses it as one
(`Chemclaw3_ui/src/api/client.ts`: `request<SessionSummary[]>('/sessions')`), so an envelope
(`{"sessions": […], "next": …}`) would have broken every deployed client in order to add a field;
a per-row field would have had to go on `SessionSummary`, a model shared with other surfaces. A
header is additive to both — a client that does not read it sees byte-for-byte what it saw before.
A bare cursor rather than RFC 8288's `Link: <url>; rel="next"` because the UI reaches this service
through a BFF that maps `/api/sessions` onto `/sessions`: any URL this process built would name a
path the browser cannot use. The cursor is the part that is actually ours to state.

The header is set only when the page is full, so its absence means "no more". Fetching one row past
the ceiling to be certain would cost every listing an extra row to answer a question the next request
answers for free by coming back empty.

### 2. `DELETE /sessions/{session_id}` — authorized exactly as reading it is

The route resolves through the **same `resolve_session` dependency** the transcript route uses. That
identity is the decision, not a convenience: a caller who cannot read a session must not be able to
delete it, and the only way to guarantee that permanently is to have one gate rather than two that
can drift. Consequently:

- **404, not 403**, for a session that does not exist *and* for one that belongs to somebody else —
  the same refusal every other session-scoped route gives (`api/deps._refuse`). A 403 would confirm
  which ids exist and turn a delete endpoint into an enumeration oracle. The distinction survives
  server-side, where that module already logs and counts the refusal with its reason.
- No new role, no reviewer check. Deleting one's own conversation is not an operator action; the
  operator action is `make user-erase`, which is a different question with a different scope.

**A turn in flight refuses with 409.** The delete claims the session's turn slot the way
`POST /sessions/{id}/messages` claims it — the in-process lease first, then the durable
cross-process one — and holds it for the sweep, so the delete can neither land between a running
turn's tool call and the row it is about to write, nor let a turn start while the sweep runs. Both
leases are checked because they answer different questions: the durable claim is another *pod*
running the turn, the in-process one is this pod. The client's answer is the same 409 a second
concurrent turn gets, and the way through is the stop route.

**The table set is `leaver._ERASE`'s, derived rather than restated.** `_session_delete_statements()`
takes the erasure sweep's own ordered table list and pairs each table with a session-scoped
predicate; a table there that is neither session-scoped nor in `_ACTOR_SCOPED_ONLY` raises. So the
next writer to add a table to the erasure sweep is told this delete has no opinion about it yet,
instead of a session's rows quietly outliving the session. Four of the twelve are deliberately not
touched — `store`, `store_vectors`, `subscriptions`, `user_preferences` — because they are keyed by
the *person*: a memory outlives the conversation it was written in, and closing one chat must not
clear a chemist's preferences across every other one. The order is `_ERASE`'s, for `_ERASE`'s reason:
everything keyed by the session goes before the ownership row that is the only way to find it again.
It runs in one transaction, because twelve statements committing one at a time can be interrupted
after the one that deletes the ownership row — and rows nothing can reach by name are exactly what
this route exists to prevent.

`tool_result_blobs` is the one statement that is not a bare `session_id = …`. Blobs are
content-addressed, so two conversations that ran the same tool over the same arguments share one row,
and the link rows cascade with it — deleting unconditionally would take a *stored result out of a
conversation nobody asked to delete*. So a blob goes only when no other session links it. The
consequence is stated rather than hidden: this session's own link row survives when its bytes are
shared, because `infra/sql/grants/app_privileges.sql` withholds DELETE on `tool_result_links` on
purpose (a link may disappear only behind its blob). What is left is a row naming a session id that
resolves to nothing, and retention's age sweep collects it with the blob.

**The live in-process handle is tombstoned.** `_resolve_session` consults the front door's LRU
*before* the store, so a delete that only cleared the database would leave this pod serving — and
writing new messages into — a conversation whose ownership row is gone. The entry is replaced with
an owner string no principal can equal (random per delete, and truthy so the dev-mode "no recorded
owner, so anyone" branch cannot open it), and the bounded LRU ages it out. Sibling pods learn the
same way they learn about an erasure: on their next durable lookup. That residual window is the same
one `leaver` has and is not closed here; closing it needs an invalidation channel the front door does
not have.

## Consequences

- A chemist can reach every conversation they own, and remove one, without an operator.
- `GET /sessions`'s response body is unchanged, so `Chemclaw3_ui` needs no change to keep working.
  To *use* either feature the UI needs two edits of its own: forward the `after` query parameter and
  read `X-Next-Cursor` (its proxy already forwards query strings and response headers untouched), and
  whitelist `DELETE /api/sessions/{id}` in `server/routes.ts`, which today lists no DELETE for a
  session — so the route is unreachable from the browser until it does.
- Paging is a property of the durable store rather than of the `SessionOwners` protocol the front
  door types its registry as. A registry injected through `create_app(owner_store=…)` that is not
  that store answers the first page and refuses a cursor with a 422, rather than silently answering
  page one forever. Moving `page_for_owner` onto the protocol is a reasonable later change and was
  not made here: it would oblige every injected registry to implement a keyset resume it has no
  storage to do.
- The delete writes a `session.deleted` log event and no metric series. A counter would have to be
  declared in `core/metrics.py`, which this change does not touch; the log line carries the actor,
  the session and the row count in the meantime.
- `tests/test_service.py`'s session-scope inventory gains one entry. That test exists to force
  exactly this conscious update when a session-scoped route appears, and its behavioural sweep now
  proves the new route 404s for a non-owner along with every other one.
