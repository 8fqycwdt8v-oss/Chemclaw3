-- The index the evidence pack's own read needs.
--
-- `075` indexed `attempted_at`, `system` and the unsettled partial — none of which any query in
-- `src/` uses — and left `session_id` unindexed, which is the one predicate
-- `operations/evidence_pack.assemble` issues. (`effect_ledger.effects_for_session` was the second
-- caller named here; it never had one of its own and was deleted.) Measured at 50k rows
-- that read plans as a sequential scan plus a sort, and `effects` is in `retention._NOT_PRUNED`, so
-- it only grows. `db.connection` applies a statement timeout, which makes the failure mode "the
-- evidence pack raises" — on the artefact whose whole purpose is to be produced during an audit.
CREATE INDEX IF NOT EXISTS effects_session_idx
    ON effects (session_id, attempted_at DESC);
