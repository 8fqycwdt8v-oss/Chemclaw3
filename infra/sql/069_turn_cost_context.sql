-- Whether the context policy acted on a turn, and whether it ran out of room — the two facts that
-- make the policy joinable to the bill it exists to reduce.
--
-- `turn_costs` records what a turn spent: input, output, cache-read and cache-write tokens, per
-- correlation id, per actor, per profile. `chemclaw_context_compactions_total` records that the
-- context policy fired. Nothing joined them, so the one question a deployment actually asks about
-- compaction — "what does it cost us, and is it working" — had no answer in either place: the
-- counter is fleet-wide and unlabelled, and the ledger did not know compaction existed.
--
-- The second column is the one that matters for an alert. Measured through a compiled graph, a
-- thread of 100,081 estimated tokens (~224,000 billed) over both context triggers moved *neither*
-- counter, because both edits ran and reclaimed nothing: `ClearToolUsesEdit` had exactly `keep`
-- candidates and the conversation window cannot cut past the newest group. So "the policy did not
-- fire" and "the policy could not do anything" were the same reading, and the second is the turn
-- that is about to fail at the provider's context limit. `context_unreducible` names those turns,
-- which is what lets an operator find the sessions and the profiles that produce them rather than
-- only the rate.
--
-- Both are booleans and both may be true of one turn: an early model call reduced the thread and a
-- later one was over the budget with nothing left to reclaim.
--
-- Additive and defaulted, per `infra/sql/README.md`: every existing row keeps its meaning and the
-- previous image can still write.

ALTER TABLE turn_costs ADD COLUMN IF NOT EXISTS compacted BOOLEAN NOT NULL DEFAULT FALSE;

-- Set when a model call in this turn went out over the conversation budget with the policy unable
-- to reduce it further (`agent/compaction.py::_record_overrun`). The leading indicator of a
-- context-length failure, and the only one this system has.
ALTER TABLE turn_costs
    ADD COLUMN IF NOT EXISTS context_unreducible BOOLEAN NOT NULL DEFAULT FALSE;
