-- The pre-execution approval a human gives a harness plan (D-137 / REV-1).
--
-- `SECURITY.md` and `docs/guides/harness-konzept.md` §6 describe an approval gate: in `plan_only` the agent
-- proposes and waits for a human before executing. In practice MAF injected a `mode_set` tool into
-- the model's own tool surface, so the agent flipped itself — and the audit middleware recorded
-- that flip under the *chemist's* Entra oid, because it attributes every tool call to the ambient
-- actor. The trail therefore showed an attributable approval with no human act behind it.
--
-- This table is the human act. A row exists only because an owner-scoped HTTP route was called by
-- an authenticated principal; the agent has no path that writes here.
--
-- **Keyed by (session_id, plan_hash), not by session.** An approval that only recorded "this
-- session may execute" would authorize whatever the plan later became: the agent could present a
-- modest plan, have it approved, rewrite its todo list, and run something else under the same
-- authorization. The hash is over the rendered todo lines the chemist actually saw, so a changed
-- plan is a different key and is unapproved until someone approves it too.
--
-- Rows are kept, never updated: a decision is a record of something a person did at a moment,
-- and a second decision on the same plan is a second record. `decided_at` orders them, and the
-- read path takes the latest — so a rejection after an approval revokes it, which is the
-- behaviour a human would expect from clicking "no" second.
--
-- Deliberately not folded into `session_turns` (liveness, disposable) or `session_owners` (the one
-- authorization row read on every cache miss): this is durable evidence with a different lifetime
-- from both.
CREATE TABLE IF NOT EXISTS plan_approvals (
    id         BIGSERIAL   PRIMARY KEY,
    session_id TEXT        NOT NULL,
    plan_hash  TEXT        NOT NULL,
    actor      TEXT        NOT NULL,
    approved   BOOLEAN     NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The read path is always "the latest decision for this session and plan", so the index carries
-- the ordering column and the lookup needs no sort.
CREATE INDEX IF NOT EXISTS plan_approvals_lookup
    ON plan_approvals (session_id, plan_hash, decided_at DESC);
