-- Which release owns this database, so a second one cannot start on it by mistake
-- (D-2026-10-15-tenancy-is-a-column-and-a-policy-switched-on-by-the-operator).
--
-- Two ChemClaw releases that share a Postgres database share every table: one release's retention
-- sweep prunes the other's live threads, and their rows are told apart by nothing. The chart can
-- name the Temporal namespace a release owns and cannot see the database, so the database says it
-- itself. `core/migrate.py` stamps this one-row table with the release's identity
-- (`settings.deployment_id`, which the chart derives from the Helm release's namespace and name)
-- the first time a release migrates, and the migrate hook, the front door and every worker compare
-- it with their own on start (`core/deployment.py`). A different identity is refused unless
-- tenancy is on, the policies are enforced and the second release owns tenants of its own.
--
-- One row, by construction: the key is a constant `true` the CHECK admits only once. The runtime
-- role reads it and cannot write it (`schema_migrations`' standing), so a request cannot move the
-- stamp. Re-stamping after a release is renamed is an operator statement under the owning
-- principal: `UPDATE deployment_id SET deployment_id = '<new identity>'`.
CREATE TABLE IF NOT EXISTS deployment_id (
    singleton     BOOLEAN     PRIMARY KEY DEFAULT true CHECK (singleton),
    deployment_id TEXT        NOT NULL CHECK (deployment_id <> ''),
    stamped_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The release that serves a tenant, when more than one shares the database. NULL (the default, and
-- every tenant of a single-release deployment) means any release may.
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS deployment_id TEXT;
