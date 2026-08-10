-- What a conversation list needs beyond "this session exists": a name, and a last-activity.
--
-- `GET /sessions` returned `(session_id, created_at)`, which is not enough to render the sidebar it
-- exists to render. The companion UI showed every restored conversation as the same placeholder
-- string and sorted them by the wrong date, and it could not do better from the outside: a title
-- has to come from the conversation's content, and "when was this last used" is not a fact
-- `session_owners` holds.
--
-- **The title is a column here rather than a query over `session_messages`.** Deriving it in SQL
-- means reaching into the stored MAF payload for the first user message's text, and `008_sessions`
-- is explicit that the store does not interpret that JSONB — "a MAF message-shape change is a value
-- change, not a schema change". A SQL expression that reads `message->'contents'` would quietly
-- convert every future MAF shape change into a broken conversation list. The front door already has
-- the user's message as a plain string when it accepts a turn, so it writes the title from there
-- and nothing has to parse anything.
--
-- Nullable, and for the same reason `owner` and `profile` are: a session that has never had a turn
-- genuinely has no title, and NULL is the honest value. This follows 021's argument exactly — this
-- row is "the facts about a session that must survive the LRU", and a name is one of them.
ALTER TABLE session_owners ADD COLUMN IF NOT EXISTS title TEXT;

-- The last-activity half is derived, not stored: `max(session_messages.created_at)` per session.
-- Deriving beats denormalising here because the write path for a turn already inserts into
-- `session_messages`, and a mirrored `updated_at` on `session_owners` would be a second write per
-- turn that can silently fall out of step with the first.
--
-- This index is what makes deriving it cheap. The listing runs one lateral `max(created_at)` per
-- session the caller owns, and neither existing index serves that: `session_messages (session_id,
-- id)` from 008 is ordered by insertion id, and `(created_at, session_id)` from 022 leads with the
-- wrong column for a per-session lookup. With this one each lookup is a single backwards index
-- probe instead of a scan of that session's rows — the same reasoning 022 gave when retention grew
-- a new access path.
CREATE INDEX IF NOT EXISTS session_messages_session_recent_idx
    ON session_messages (session_id, created_at DESC);
