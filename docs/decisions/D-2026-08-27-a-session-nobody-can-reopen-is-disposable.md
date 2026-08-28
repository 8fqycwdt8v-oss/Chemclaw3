# D-2026-08-27-a-session-nobody-can-reopen-is-disposable — `session_owners` is pruned behind everything it keys

**Status:** accepted · **Date:** 2026-08-27

## Context

`docs/planning/BACKLOG.md` carried one row for two tables — "`session_owners` grows without any
age-based disposal, and `session_turns` only partially does" — and asked for a *policy decision*
rather than a code change made unilaterally. `infra/sql/README.md`'s own `session_owners` row had
been flagging the same thing ("survives its session's pruned history; BACKLOG"), and
`durable/retention.py`'s `_NOT_PRUNED` register named both tables and delegated the answer back to
that row. Nothing in the tree held the answer.

**What the two tables actually do, measured rather than assumed.**

`session_owners` is one row per session id, written once at creation. It is the row that makes a
session *reopenable*: `api/deps.py::_rehydrate_session` looks the owner up on a live-cache miss and
answers 404 for an id this table does not hold. It also carries the session's `profile`, which is
attenuation-only (REV-14), and its `title`.

It is written **per session id created, not per conversation**. The companion UI creates the session
on the first keystroke to save a round-trip on the first message, which `_OWNER_LIST`'s own comment
records — and that listing already drops a session with no messages, through the lateral join's
`ON m.updated_at IS NOT NULL`. So an abandoned draft leaves a row that is invisible in the session
list, reachable only by an id a client still remembers, and deleted by nothing: the only `DELETE`
against this table before today was `agent/leaver.py`'s actor-scoped erasure, which a deployment
nobody leaves never runs, and `session_store.delete_session`, which a person has to ask for.

Measured on a seeded database: 200 000 ownership rows are **24 MB** of total relation size, i.e.
**124 bytes per session id**, index included. Small per unit; unbounded in the count, and the count
is driven by keystrokes rather than by conversations.

`session_turns` is different, and the backlog row's correction is the important half. It is a
*lease* keyed by `session_id` — `_TURN_RELEASE` deletes the row on every clean release, and
`_TURN_CLAIM`'s `ON CONFLICT … DO UPDATE` overwrites an expired one **in place**. So it does not
accumulate under normal operation. What survives is the lease a SIGKILLed worker never released, on
a session nobody uses again: one row, not growth.

## Decision

**A `session_owners` row is disposable exactly when nothing can reopen the session and nothing is
left to reopen into.** Concretely, `durable/retention.py::_prune_session_owners` deletes a row when
all of:

1. `created_at` is past the retention window, and
2. no table in `_SESSION_SCOPED_ROWS` holds a row for that session — `session_messages`,
   `session_events`, `tool_result_links`, and the checkpointer's `checkpoints`,
   `checkpoint_blobs`, `checkpoint_writes` — and
3. no **live** turn lease names it (`session_turns.expires_at > now()`).

The abandoned lease of such a session is deleted with it, in the same statement. A live lease is
never touched, and no lease is collected on a clock of its own.

`session_owners` is **last in `_PRUNABLE`**, and that position is the decision as much as the
predicate is. `_NOT_PRUNED`'s two entries are updated to say this instead of pointing at a backlog
row, and `tests/test_retention.py` pins both halves.

## Why

**1. The ordering is not a nicety — the row is the only way back to everything else.** Every
session-scoped sweep in this system starts from `session_owners`: `leaver.erase_actor` selects
session ids out of it (`_SESSION_SCOPED`), and `session_store.delete_session` deletes one session
by that row. An ownership row deleted while a checkpoint, an unconsumed push-back event or a stored
tool result still names the session does not merely orphan that row — it puts it beyond *erasure*,
the one sweep that must never be able to miss something. So the guard is the reachability set, and
it is derived from `leaver._ERASE` by a test rather than transcribed: the next table added to the
erasure sweep fails `test_the_reachability_guard_names_every_session_scoped_erasure_table` instead
of being silently outlived by the row that finds it.

**2. A session whose conversation expires in this pass is forgotten in this pass, and that is
correct.** The ordering hazard is real: retention prunes `session_messages` by age, so a session
becomes empty and eligible in the same sweep. Giving the ownership row its own, longer window was
considered and rejected. What is left after the conversation goes is a shell: it is not in the
session list (it never was, once its messages were gone), and reopening it by a remembered id
renders a blank transcript under an old title. Keeping the shell is precisely the unbounded growth
this decision closes, and it would be kept **forever**, because after its messages are pruned
nothing else ever changes about it. The direction that would be wrong is the other one — the
ownership row going *first* — and that is what the position in `_PRUNABLE` and the guard prevent.

**3. The window is the conversation's, deliberately, and no new setting was added.** The guards, not
the clock, are what hold a row that still has rows; the window is only a floor under "how long after
it was created may an *empty* session be forgotten". The one number a deployment already states
about how long a conversation is kept is the honest floor for that: a session may not be forgotten
sooner than the conversation in it would have been. A second `retention_session_owners_days` could
be set equal to the conversation window (no effect), longer (a delay before an already-empty shell
goes), or shorter (no effect either, because the anti-joins hold the row) — three values, one
outcome, and a fourth knob for an operator to keep in step. `retention_enabled` and a window of 0
still mean nothing is deleted, as for every other table.

**4. A live lease is what says a turn is running, so it is the guard for the case age cannot see.**
A turn's transcript is written *after* the answer exists (`api/runner._record_transcript`), so a
session resumed from an old, empty ownership row genuinely holds no rows anywhere while its turn is
running. Without this arm the sweep would delete the ownership row of a conversation in progress and
leave a transcript nothing can find. An *expired* lease is not that case: it is the crash artifact
every other reader already treats as dead — `_TURN_CLAIM` takes an expired row over unconditionally
— so it does not protect the row and it is swept with it. Deleting a lease that is merely late
would also manufacture a false takeover: `api/state.py::_hold_turn_claim` counts
`chemclaw_turn_claims_lost_total` and stops beating when a refresh matches no row.

**5. The predicate is re-checked in the `DELETE`, which is what closes the race.** The candidate
query and the deletion are two statements, and under `READ COMMITTED` each takes its own snapshot.
Re-asking at delete time means a session that claimed a lease — or wrote a message, or a checkpoint
— between them is no longer disposable at the moment of deletion, and a turn always claims its lease
before it runs. The ownership row and its lease go in one data-modifying CTE, so a lease cannot be
committed apart from the row it belongs to.

**6. No migration, because the index that looked obvious changes no plan.** Measured on 200 000
ownership rows (20 000 abandoned drafts, 180 000 with history), cap 501: `Index Scan using
session_owners_pkey` under four merge anti-joins, **1.9 ms**, 110 buffers. Adding
`session_owners (created_at)` produced the identical plan at **1.8 ms** — the `ORDER BY session_id`
is what drives the scan, and the cutoff is not selective, since in the case that matters nearly
every row is older than the window and the anti-joins are the filter. On a *drained* backlog
(180 000 live sessions, nothing disposable) the plan becomes one parallel hash anti-join at
**147 ms**, with that index present and unused. A migration that changes no plan is write
amplification on the session-creation path in exchange for nothing. The reserved migration number is
therefore not spent.

## Consequences

- A deployment that states `retention_session_messages_days` now also disposes of the ownership
  rows of sessions that hold nothing — the abandoned drafts, which are the bulk of them.
- A session whose id a client still remembers answers 404 after its shell is forgotten, instead of
  reopening as an empty conversation under its old title. That is the same answer the session list
  has been giving all along.
- `session_turns` gains no window of its own. Its row count is bounded by `session_owners`' policy,
  because a lease is only ever claimed on a session that has an ownership row, and it is swept with
  that row. The one leak left is a lease claimed in the instant *between* the delete and its commit,
  on a session whose ownership row has just gone — a single row, never duplicated (the claim
  overwrites in place), for a race that requires a turn to start on a session past its window with
  nothing in it. It is named here rather than closed with a second mechanism nothing could test.
- The pass is capped by `retention_max_sessions_per_pass` and reports `owners_deferred` alongside
  `sessions_deferred` and `threads_deferred`, as a probe rather than a remainder.
- What is **not** decided here: the eight other tables `_NOT_PRUNED` still marks "nothing bounds
  it". This closes one row of that register, not the register.
