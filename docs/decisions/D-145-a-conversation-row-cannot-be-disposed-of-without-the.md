# D-145 — A conversation row cannot be disposed of without the rows it is paired with

**Status:** accepted · **Context:** found while verifying REV-4. The performance finding was about
an unbounded read; underneath it was a **data-corruption path that is already shipped**, and this
ADR is about that.

### The asymmetry

`agents/message_pairing.py` enforces one rule on stored history: every tool call carries its result.
It enforces exactly half of it.

- `unmatched_call_ids` reports **calls no result answers**.
- `strip_call_ids` removes contents where `type == "function_call"`.
- `get_messages` runs both on every read and writes the repair back.

So an orphaned **call** is detected and healed automatically. An orphaned **result** — a
`tool_result` whose `tool_use` is gone — is reported by neither and removed by neither. The API
rejects that thread exactly as hard as the converse, so it is a poison pill replayed on every
subsequent turn, and unlike the case the module was written for it has **no self-heal path at all**.
The session is bricked until somebody edits the database.

### What already creates one

`workflows/retention.py` pruned `session_messages` with a single
`DELETE ... WHERE created_at < now() - interval`. That statement has no knowledge of tool-call
groups. A turn's rows are written together and share a `created_at`, so the common case is safe by
accident — but a cutoff is an instant, and a pair *can* straddle it: a call retried across the
window boundary, a mid-turn-resume interleaving, a clock that moved. When it does, the older half
goes and the newer half stays. If the older half is the call, the survivor is a stranded result.

**A cleanup job could brick a session, permanently, with no way back.** Dormant only because
`retention_session_messages_days` defaults to 0. That is precisely the condition under which the
last review found it cheapest to fix something (REV-12, calibration): before anyone has switched it
on and before any data has been lost.

### Decision

**One primitive, `agents.message_pairing.droppable_rows`, applied by everything that deletes
conversation rows.** Rows are joined into components by shared `call_id` in either direction; the
relation is transitive, so one assistant message carrying three parallel calls binds all three
result rows into a single component. A component survives or dies whole.

**It contracts; it never expands.** A component with even one row outside the caller's candidate set
is dropped from the answer entirely, rather than pulling its remaining rows in. This direction is
the whole safety argument: expanding would let an age cutoff reach *forward* and delete a live
result from a recent turn, whereas contracting can only ever return a subset of what the caller
already chose. The worst case is a straddling group surviving one more pass — harmless, and
self-correcting once the partner also ages out.

The closure takes the session's **whole** history, not just the candidates. A candidate's partner
frequently is *not* a candidate — that is the case worth catching — and a partial view would report
a split component as safe, which is the original bug in a new place.

`unmatched_result_ids` joins it as the mirror of `unmatched_call_ids`, and is **deliberately not
wired into the read-time repair**. Healing a stranded result would destroy evidence and, worse,
would mask a regression in whatever produced it. Its job is to be the assertion: code that deletes
conversation rows proves it never leaves one, rather than relying on something cleaning up after.

Retention keeps its policy shape — `_PRUNABLE` stays the closed, explicit set — but
`session_messages` routes through a per-session pass: find the sessions with expired rows, read each
one whole, apply the closure, delete. Three statements per session instead of one across the table,
because the decision is not expressible in SQL: whether an expired row may go depends on rows that
may be newer than the cutoff.

**`infra/sql/022`** adds `(created_at, session_id)`. The old sweeping `DELETE` seq-scanned once per
pass and nobody minded; the new first step is a `created_at` predicate that now runs inside a 600 s
activity budget against the one table that grows without bound.

### What this deliberately does not do

- **It does not touch `get_messages`.** Repair-on-read still heals genuinely broken sessions and
  still writes back, which is wanted. No `LIMIT`, so the hazard D-143 documented stays closed.
- **It does not heal sessions already bricked in the wild.** Adding stranded-result stripping to the
  read repair is tempting and is recorded in `BACKLOG.md` instead: shipping it alongside this change
  would **mask** a regression in the closure rather than surface it. It earns its own argument.
- **It does not bound the read.** That is REV-4's performance half, which needs the durable
  compaction this primitive was built to serve, and which is a separate ADR.
