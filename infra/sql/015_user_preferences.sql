-- Per-user working preferences (gap AGT-4).
--
-- Every memory layer in the system is corpus-level: `campaign`, `playbook`, `optimization-campaign`
-- and `interaction` notes all describe the *chemistry*, shared by everyone. Nothing remembered
-- *this chemist* — their project, their preferred solvent system, the units they think in, or that
-- they already rejected an analogy last week. Session identity existed (`session_owners`), so the
-- key was available; only the layer was missing.
--
-- Deliberately NOT knowledge-graph notes. A preference is personal, revisable, and uninteresting to
-- anyone else, so routing it through the PR-gate would ask a human to review "Anna prefers 2-MeTHF"
-- — noise that would erode the seriousness of the gate itself (D-005). The graph holds what the
-- organisation knows; this holds how one person works.
--
-- `owner` is the Entra oid. One row per (owner, key) so setting a preference is an idempotent
-- upsert, and `updated_at` lets a surface show (and a retention policy age) stale entries.
CREATE TABLE IF NOT EXISTS user_preferences (
    owner      TEXT        NOT NULL,
    key        TEXT        NOT NULL,
    value      TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner, key)
);
