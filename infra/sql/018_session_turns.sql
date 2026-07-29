-- One turn at a time per session, across every process (D-121).
--
-- The front door already refuses a second concurrent turn on one session with a 409, because two
-- turns driving `agent.run` against the same conversation thread interleave their messages into
-- one history and can leave an orphaned tool_use behind. That guard was a Python `set` in one
-- process's memory — which is exactly as wide as one process. The shipped Helm chart runs the
-- front door at `minReplicas: 2`, so two turns on one session landing on different pods have
-- always both been admitted; raising `service_uvicorn_workers` above 1 would add the same hazard
-- inside a pod. This table is that guard, at the width the deployment actually has.
--
-- A row is a *lease*, not a lock: `holder` names the process that owns the turn and `expires_at`
-- is when the claim stops being believed. That is what a Postgres advisory lock (or a
-- `SELECT … FOR UPDATE`) could not offer — both are connection- or transaction-scoped, so holding
-- one for a turn's whole duration means pinning a pooled connection for minutes, re-creating the
-- connection starvation this branch exists to remove. A lease is three short statements (claim,
-- refresh, release) that each borrow a connection and give it straight back, and it ages out on
-- its own when a worker is SIGKILLed mid-turn — where a lock held by a dead connection needs the
-- server to notice the socket died, and an in-memory set needed a restart to clear.
--
-- Deliberately its own table rather than a column on `session_owners`: that row is the single
-- security-relevant fact the front door authorizes every request against, and it is read on every
-- cache miss. Putting a per-turn heartbeat on it would rewrite (and bloat) the authorization row
-- several times per turn, for liveness data whose whole point is that it is disposable.
CREATE TABLE IF NOT EXISTS session_turns (
    session_id TEXT        PRIMARY KEY,
    holder     TEXT        NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
