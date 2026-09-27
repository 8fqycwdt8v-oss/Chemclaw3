-- Who wrote each message of a transcript: the person it was written for, and the agent that wrote
-- it (`src/chemclaw/core/authorship.py`, D-2026-09-27-an-author-is-a-person-and-an-agent).
--
-- `session_messages` had no actor column, so a transcript could not say whose words a message
-- was — true of every session and harmless while a session has one owner, and the first thing a
-- shared session needs (the multi-human row in `docs/planning/BACKLOG.md`, and the sender-governs
-- decision that builds on it). Three subsystems wanted the same missing column; the shape was
-- decided once and this table takes it in the two names `audit_events` already spells it in:
--
--   actor  the human principal on whose behalf the message was written — NULL = not recorded
--   agent  the agent that wrote it; NULL = a human wrote it directly, '' = an agent, unnamed
--
-- **Both nullable, and that is rule 89 rather than caution.** A default is an assertion about every
-- row already in the table: `NOT NULL DEFAULT ''` on `agent` would say an agent wrote every chemist's
-- question ever stored, and any default on `actor` would name somebody. So the columns arrive empty
-- and the backfill below fills what the database can actually establish.
ALTER TABLE session_messages ADD COLUMN IF NOT EXISTS actor TEXT;
ALTER TABLE session_messages ADD COLUMN IF NOT EXISTS agent TEXT;

-- **The backfill, and why each half is a fact rather than a guess.**
--
-- `actor` is the session's owner. Every session this table has ever held had exactly one person in
-- it — the owner gate refuses anybody else (`session_store.owner_permits`) — so every message in it,
-- the chemist's and the agent's alike, was written on that person's behalf. That is what the column
-- means, and it is read from `session_owners` rather than inferred. A session with no ownership row
-- (an orphan a deletion left, or one written before ownership was recorded) or an unattributed one
-- (`owner IS NULL`) stays NULL: nobody is invented.
--
-- `agent` is read off the speaker the row itself names, in either stored shape — LangChain's `type`
-- or MAF's `role` (`session_store.message_from_row` reads both; `_DEGRADED_CLASSES` is the same
-- vocabulary). A human-labelled row keeps NULL: the chemist wrote it. Every other row gets '': the
-- agent's answer, its tool calls and their results were written by the one agent a chemist has
-- ever talked to in a stored transcript, and '' is the audit trail's own word for that agent
-- (D-2026-09-06-the-one-agent-that-exists-is-named-in-the-trail). A row whose label is unreadable
-- is treated as the agent's, which is how the transcript already renders it
-- (`_degraded_class` falls back to an assistant bubble) — so the column agrees with what a chemist
-- is shown rather than contradicting it. One caveat is inherited, not introduced: a MAF-era `user`
-- row is not provably something a person typed (the stated-quote ambient excludes those rows for
-- that reason), and it backfills as the chemist's, which is the label it has always carried.
--
-- **One statement, so the table is rewritten once — and the rewrite is the whole cost.** An
-- `UPDATE` takes `ROW EXCLUSIVE` and row locks, not a table lock, so turns keep appending while it
-- runs. What it costs is a new version of every row (dead tuples double until autovacuum reclaims
-- them) and the migration transaction's duration. Measured on this repository's Postgres image
-- (PostgreSQL 16, a laptop's Docker VM), one million rows over 10,000 sessions, 350-character
-- payloads, `VACUUM ANALYZE` first: **49.4 s** for this statement, **42.3 s** for the same backfill
-- written as `UPDATE … FROM session_owners`, and **41.0 s** for `SET actor = NULL, agent = NULL`,
-- which computes nothing — so the lookup and the label test are noise and the row rewrite is the
-- bill, whatever the statement is written as. The pre-upgrade hook Job's
-- `migrateJob.activeDeadlineSeconds` (900 s, retries included) is what a large transcript table
-- has to fit inside: raise it before this release on a database holding more than a few million
-- stored messages, as `values.yaml` already says for a large index build.
--
-- Idempotent: it touches only rows the migration has not filled, so a replay after a partial
-- restore is a no-op on everything already stamped.
UPDATE session_messages m
   SET actor = (SELECT o.owner FROM session_owners o WHERE o.session_id = m.session_id),
       agent = CASE
                   WHEN coalesce(m.message ->> 'type', m.message ->> 'role') IN ('human', 'user')
                       THEN NULL
                   ELSE ''
               END
 WHERE m.actor IS NULL AND m.agent IS NULL;
