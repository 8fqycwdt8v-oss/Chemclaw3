-- The column 066 added is reserved and unread, and the deployed comment says otherwise.
--
-- 066 shipped `reaction_records.retracted_at` with a `COMMENT ON COLUMN` describing a sweep that
-- sets it: "Set only from a retraction the source reports — never inferred from an entry's absence
-- from an export". That tier was deleted on review the same day
-- (`D-2026-08-27-a-withdrawn-entry-is-a-fact-the-sync-must-carry`): it had no implementer, the
-- production adapter wrapper hid the capability from the walk that looks for it, and — the finding
-- that decided it — with both of those fixed a retracted reaction was still returned by the
-- unfiltered evidence sweep and by the agent's own `similar_reactions`.
--
-- Editing 066 in place would change nothing, because 066 is already applied: its comment is in the
-- deployed catalogue, where a DBA reading `\d+ reaction_records` finds it. So the correction is a
-- migration, and it is comment-only.
--
-- The column itself stays. Dropping an applied column is destructive under
-- `D-2026-08-04-the-schema-only-goes-forward`, it costs nothing where it sits, and a real
-- implementation — one that also teaches the retrieval path and `similar_reactions` to honour a
-- withdrawal — reuses it rather than minting a second.
COMMENT ON COLUMN reaction_records.retracted_at IS
    'Reserved and unread. Nothing writes this column: the ingest tier that would have set it was '
    'removed on review because the readers that would have to honour a withdrawal — the unfiltered '
    'retrieval sweep and the `similar_reactions` tool — do not, so a sweep that fired would have '
    'been a control that reads as enabled and is not. See '
    'docs/decisions/D-2026-08-27-a-withdrawn-entry-is-a-fact-the-sync-must-carry.md. A future '
    'implementation reuses this column and must move those readers with it.';
