-- Several people in one session: who the owner has let in, and whose turn wrote the plan
-- (D-2026-09-27-in-a-shared-session-the-sender-governs).
--
-- **`session_members` is the owner's list of who else may reach a session.** Until now the session
-- gate was an equality — `session_owners.owner = <caller>` (`session_store.owner_permits`) — so a
-- session had exactly one person in it. A membership row widens *who may reach* the session and
-- nothing else: every turn still runs as the person who sent it, so a member acts with their own
-- roles, memories and spend caps and never with the owner's. Only the owner writes this table
-- (`PUT/DELETE /sessions/{id}/members/{actor}`); a member may remove only themselves.
--
-- **`plan_authors` is whose turn last wrote a session's plan**, keyed by the plan's identity
-- (`plan_gate.plan_identity`), so "only the plan's author may approve it" has a column to read. The
-- plan gate stamps it when `write_todos` runs, as the turn's own actor; the last writer of an
-- identity is its author, because the turn that last wrote the plan is the one that stands behind it.
--
-- **Both cascade from `session_owners`.** A membership or an authorship means nothing once the
-- session is gone, and cascading is what keeps the three disposals that remove an ownership row —
-- `DELETE /sessions/{id}`, the retention sweep's `_prune_session_owners` and an owner's erasure —
-- from each needing a second statement that one of them would eventually forget. A cascade runs
-- with the referencing table owner's privileges, so the runtime role needs no DELETE for it; the
-- DELETEs it does hold are for removing one member and for erasing one person's rows from sessions
-- somebody *else* owns (`agent/leaver.py`).
--
-- **No default on any person column** (`tasks/lessons.md` rule 89): both tables are new, so there
-- is no existing row for a default to make a claim about, and neither column may be absent.
CREATE TABLE IF NOT EXISTS session_members (
    session_id TEXT        NOT NULL REFERENCES session_owners (session_id) ON DELETE CASCADE,
    actor      TEXT        NOT NULL,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, actor)
);

-- The two reads that go by person rather than by session: "which sessions am I a member of"
-- (`GET /sessions/shared`) and an erasure's "every membership this person holds".
CREATE INDEX IF NOT EXISTS session_members_actor_idx ON session_members (actor);

CREATE TABLE IF NOT EXISTS plan_authors (
    session_id  TEXT        NOT NULL REFERENCES session_owners (session_id) ON DELETE CASCADE,
    plan_hash   TEXT        NOT NULL,
    actor       TEXT        NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, plan_hash)
);

-- An erasure removes a departing person's authorship from sessions they do not own.
CREATE INDEX IF NOT EXISTS plan_authors_actor_idx ON plan_authors (actor);
