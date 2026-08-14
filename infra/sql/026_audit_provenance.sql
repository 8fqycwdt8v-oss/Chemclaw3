-- Join the audit trail to the conversation that caused it, and version the (since-removed) hash
-- chain so that doing so did not invalidate the trail it is joining (D-166).
--
-- `correlation_id` has always been minted per turn (`api/runner.py`) and stamped on `audit_events`,
-- on a connector job's Temporal memo, and on the connector request header. It was stamped on
-- nothing holding the user's words. D-157 closed that for durable jobs — `job_records` carries the
-- rationale, the session and the correlation id — but a durable job is the minority of the trail.
-- `gather_evidence`, `predict_pka`, `suggest_next_experiment` and `propose_knowledge_note` are all
-- ordinary tool calls, and for those "which conversation was this?" had no answer: `audit_events`
-- had no `session_id`, `session_messages` had no `correlation_id`, and no table mapped one to the
-- other. The trail could prove *that* a tool ran and never *why*.
--
-- **Why the chain needed a version** (historical — the chain is gone, and `chain_version` is now
-- an unwritten column at its default). `agents.audit_store.chain_hash` hashed
-- `{"prev": prev_hash, "event": event.model_dump()}`. `model_dump()` is the whole model, so adding
-- a field to `AuditEvent` changes the bytes hashed for every row — including rows written before
-- this migration, whose stored `row_hash` was computed over the old field set. Verification would
-- fail on the entire history, and a trail that reports itself tampered with is worse
-- than one that reports nothing: the first thing an auditor asks is which of the two happened.
--
-- So each row records which field set its hash covers. `chain_version = 1` is everything written
-- before this migration; `2` adds `session_id` and `purpose`. The verifier dumps only that
-- version's fields, so history stays verifiable across a schema change — which is the property a
-- hash chain is supposed to have and would have quietly lost.
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS session_id    TEXT     NOT NULL DEFAULT '';
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS purpose       TEXT     NOT NULL DEFAULT '';
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS chain_version SMALLINT NOT NULL DEFAULT 1;

-- "Everything that happened in this conversation" is the reconstruction question, so it is the one
-- that gets an index.
CREATE INDEX IF NOT EXISTS audit_events_session_idx ON audit_events (session_id);

-- The other half of the join: a turn's rows carry the correlation id the audit trail records, so a
-- tool call reaches the message that prompted it and a message reaches the calls it caused.
-- Defaulted to '' rather than backfilled: rows written before this migration genuinely have no
-- correlation id, and inventing one would make an unanswerable question look answered.
ALTER TABLE session_messages ADD COLUMN IF NOT EXISTS correlation_id TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS session_messages_correlation_idx
    ON session_messages (correlation_id) WHERE correlation_id <> '';
