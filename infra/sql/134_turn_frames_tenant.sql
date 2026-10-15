-- The frames a running turn relays between replicas carry a tenant's answer text
-- (D-2026-10-15-tenancy-is-a-column-and-a-policy-switched-on-by-the-operator).
--
-- 131 left `session_turn_frames` global on the reasoning that fleet coordination holds leases, not
-- content. A frame is content: the SSE frame exactly as the turn's own readers receive it. So the
-- frames are scoped like every other table that holds a chemist's words, and what is read across
-- tenants by design is only the request that names which frames to produce.
--
-- `session_turn_remotes` stays global: the replica that holds a turn polls the requests addressed to
-- every turn it holds, whichever tenant they belong to. The request now records the tenant it was
-- asked in (`request_tenant`, a plain column, no policy), and the holder binds that tenant while it
-- writes the request's frames, so the policy on the frames is met by the asker's own tenant and by
-- no other. A request written by the previous image carries `default`, which is right while tenancy
-- is off.
ALTER TABLE session_turn_remotes ADD COLUMN IF NOT EXISTS request_tenant TEXT NOT NULL DEFAULT 'default';

DO $$
DECLARE
    scoped text[] := ARRAY['session_turn_frames'];
    t text;
BEGIN
    FOREACH t IN ARRAY scoped LOOP
        PERFORM chemclaw_scope_table(t);
    END LOOP;
END
$$;
