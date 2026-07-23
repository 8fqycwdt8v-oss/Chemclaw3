-- Tamper-evident hash chain over the GxP audit trail (plan F10-G1; completes the step deferred
-- in 006_audit_events.sql and D-034).
--
-- Each appended row carries `prev_hash` (the previous row's `row_hash`) and `row_hash`
-- (a SHA-256 over prev_hash + this row's audited fields, computed by
-- agents.audit_store.chain_hash). Because every row commits the hash of the one before it,
-- modifying, reordering, or interior-deleting a row — or deleting the leading (genesis) rows —
-- breaks the chain, detectable by `python -m scripts.verify_audit_chain` (`make audit-verify`)
-- without trusting the store. Deleting the trailing rows (tip truncation) is the one alteration the
-- chain alone cannot catch (it needs an external count anchor — see that module's known-limit note).
--
-- Both columns default to '' so rows written before this migration (there are none in a fresh
-- deployment, but a running one may have some) remain valid; the verifier treats a leading run of
-- empty-`row_hash` rows as pre-chain and begins checking at the first chained row. New inserts
-- always set both explicitly. The writer serializes appends with a transaction advisory lock so a
-- concurrent insert cannot fork the chain.
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS prev_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS row_hash  TEXT NOT NULL DEFAULT '';
