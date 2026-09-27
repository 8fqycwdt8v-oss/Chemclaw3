# D-2026-09-27-in-a-shared-session-the-sender-governs — several people in one session: whose authority a message carries

**Status:** accepted · **Date:** 2026-09-27 · **Decided by** the owner (2026-09-26, final) ·
**Works** the `BACKLOG.md` row *"Several humans in one session is five pieces, and the policy one
has to be settled first"* (issue #479) — its policy piece and its participants piece; the queued
turn and reader fan-out stay on the row · **Builds on**
`D-2026-09-27-an-author-is-a-person-and-an-agent` (a message's `actor` is its sender) and
`D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor` (authority is never manufactured by a
hop).

## Context

A session had exactly one person in it: every session-scoped route resolved through
`session_store.owner_permits`, an equality against `session_owners.owner`. The scoping record for
several people in one session (`docs/archive/PLAN-2026-09-14-multiplayer-and-the-open-delegation-questions.md`)
named three authority questions that had to be answered before any schema, because an answer
chosen afterwards is a migration:

1. whose roles govern a tool call a message causes;
2. whose `/memories/` (and `recall_*` stores) a turn loads — they are namespaced per actor, so a
   shared session loads none, the sender's, or a session tier that does not exist;
3. whether B may approve a plan A's message produced.

## Decision

**The sender governs each message, and only a plan's author may approve it.** Concretely:

1. **A turn runs as the person who sent it — never as the owner.** Identity, roles, tool gates
   (`authz`), expensive-trigger gates, spend and budget caps, the per-actor concurrent-turn cap,
   `/memories/` and every `recall_*` store are all read off the turn's ambient actor, and
   `api/routes/turns.post_message` binds the *request's* principal there. This needed no new code —
   it was already the only identity a turn could carry — and it is now a pinned property rather
   than an accident: `tests/test_shared_sessions.py` drives a member's turn in the owner's session
   and reads the actor, the roles, the memory namespace and a recalled preference from inside the
   running graph. A session's *profile* is the owner's choice and still applies, because a profile
   only ever narrows.

2. **A plan is decided only by its author**, and the author is whoever's turn last wrote it.
   `plan_gate.enforce_plan_approval` stamps the turn's actor on `plan_authors` (migration 110)
   whenever `write_todos` runs, keyed by `plan_identity`; `POST /sessions/{id}/plan/decision`
   answers 403 to anybody else. Where no author is recorded (a plan from before this, or a stamp
   that could not be stored) the old rule stands — the owner decides.

3. **An approval authorizes only its approver's turns** (`plan_gate.approval_binds`). With one
   person per session this changes nothing. With several, the session reading would let a member's
   turn act under the owner's yes, which is exactly what (1) forbids, so `approved_scope` returns no
   scope when the turn's actor is not the approver. This is also what makes the owner fallback in
   (2) safe: the owner deciding an unattributed plan authorizes only the owner's own next turn.

4. **Who may reach a session is the owner's decision: an explicit membership list.**
   `session_members` (migration 110), written only by `PUT/DELETE /sessions/{id}/members/{actor}`.
   `api/deps._resolve_session` admits the owner or a member (`session_members.participant_permits`,
   which the agent's own session-scoped read in `agent/evidence_tools.py` shares); a stranger still
   gets the same 404 an unknown id gets. Membership is read per request, so removal takes effect on
   the next one. **Membership is reach, not authority**: a member may read the transcript, send
   into the session, upload attachments and decide their own plans. Deleting and forking the
   session, admitting somebody and removing somebody else stay the owner's (403 to a member); a
   member may remove only themselves; a member stops only their own turn, the owner any.

### Retention, erasure and the claim coverage

- Both new tables **cascade from `session_owners`**, so deleting a session, the retention sweep's
  `_prune_session_owners` and an owner's erasure take them with no second statement to forget;
  `durable/retention.py` registers them as swept with the ownership row.
- `agent/leaver.py` erases a person's memberships and plan authorships **in sessions somebody else
  owns** as well, and their messages there were already reached by author (109).
- **The erasure's turn claims now span those sessions** — the gap the authorship ADR named. A
  member mid-turn in the owner's session writes exactly the rows being erased, so `_actor_sessions`
  returns the sessions the leaver is a member of beside the ones they own, and the sweep holds both.
  Only the owned ones are residue-probed: a shared session's ownership row and its other
  participants' rows are meant to survive.
- **What an erasure still cannot reach is stated, not hidden**: a member's words inside the
  *owner's* checkpointed graph state. The checkpointer holds the thread as the owner's turn state
  and a message cannot be cut from it without rewriting the owner's conversation; it goes when the
  owner deletes the session or leaves. It is named in `leaver._BEYOND_REACH` with how to find it.

## Alternatives weighed

- **The owner's roles govern a shared session.** Rejected by the owner: a member would act with the
  owner's privileges, and a role gate would stop meaning "this person may".
- **A session-scoped memory tier.** Declined: it is a store nobody's erasure names — the failure
  `agent/scratchpad.py` refuses for an anonymous prefix — and the sender's own tier already answers
  "whose memories", which is the decision. **Revisit when:** a deployment asks for shared working
  notes between members, and a design names whose erasure removes them.
- **Any member may approve any plan in the session.** Declined by the owner decision: an approval
  is consent to what a turn will do, and consent is the author's to give. **Revisit when:** a
  deployment needs a reviewer who is not the author to approve, which would be a role-gated
  approval route rather than a relaxation of this one.
- **Author = first writer of a plan identity.** Rejected for last writer: first-writer leaves a
  member whose turn re-affirms another participant's plan text unable ever to approve it, while a
  last-writer author changing on a status flip costs nothing, because an approval binds to its
  approver rather than to the author.
- **Membership cached on the live session.** Rejected: removal would wait for an LRU eviction or a
  pod roll.

## What remains on the row

A queued turn (the 409 a second concurrent message gets becomes a bounded wait), reader fan-out
(`api/detach.DetachableTurn` has one reader, so a second participant's stream steals events), and
the pending-plan inbox listing plans in sessions the caller is a member of rather than only owns.

## What was measured rather than assumed

`tests/test_shared_sessions.py`: a stranger's 404 on every session route before and after an
admission; a member's turn reading its own actor, roles, memory namespace and preferences from
inside the graph, with the owner's preference absent; a member refused on the owner's plan, the
owner refused on a member's, the unattributed fallback; an approval refusing a member's gated call
through the real middleware; `write_todos` stamping the sender, last writer winning; the owner's
acts refusing a member; revocation on the next request; and over Postgres the cascade, and an
erasure of a member that claims the shared session, removes their words, membership and authorship
there, keeps the owner's, and reports no residue.
