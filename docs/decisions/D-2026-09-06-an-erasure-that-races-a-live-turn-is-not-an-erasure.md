# D-2026-09-06-an-erasure-that-races-a-live-turn-is-not-an-erasure — the fleet-wide sweep takes the turn claim its single-session siblings already take, and counts what it could not hold

**Status:** accepted · **Date:** 2026-09-06 · **Builds on:**
D-2026-08-08-the-conversation-is-erasable-the-record-is-not (the two tiers),
D-2026-08-28-an-erasure-that-cannot-name-what-it-missed (a partial erasure must not look complete),
D-121 (the durable one-turn-per-session claim),
D-2026-09-06-a-sweep-and-a-live-turn-are-two-writers (the same two writers, one table over) ·
**Corrects** two present-tense docstrings in `api/runner.py` about the transcript and the thread.

## Context

`agent/leaver.py` is the one operation in this system whose whole purpose is completeness. Both
*single-session* paths that touch the same tables — `delete_session` and `fork_session_route` —
claim the session's turn slot and answer 409 rather than race a live turn, each citing the same
reason at length. The fleet-wide sweep took nothing.

## What was measured

`erase_actor(actor, apply=True)` run 0.6 s into a real turn on that person's thread, against a real
compiled graph and a real `SchemaStampedSaver`:

```
before:                    {owners:1, messages:2, checkpoints:8, blobs:3, writes:8}
erase report (erased):     {session_messages:2, checkpoints:15, checkpoint_blobs:5,
                            checkpoint_writes:15, session_owners:1}
right after erase:         {owners:0, messages:0, checkpoints:0, blobs:0, writes:0}
after the turn finished:   {owners:0, messages:0, checkpoints:1, blobs:1, writes:1}
second erase_actor(...):   NOTHING
thread reads back with 4 messages:
    ['first question', 'first answer', 'second question', 'second answer']
```

The residue is not empty metadata. The graph held the whole message list in memory and rewrote it,
so **the conversation from before the erasure is back in the database in full** — including the turn
the sweep had just deleted — under a `session_id` that `session_owners` no longer names. Every
session-scoped sweep in this system reaches a session *through* that row (`_SESSION_SCOPED`,
`delete_session`, `retention._DELETE_ORPHANED_SESSION_ROWS`), which is exactly the invariant
`durable/retention.py` and `agent/session_fork.py` both already quote: *"an ownership row absent
while session-scoped rows remain puts it beyond erasure."* A second erasure prints zeros, which
reads as "there were none" — the false green `leaver.py`'s own docstring calls worse than no safety
net. The sweep's `session_turns` row makes it self-blinding: it deletes the lease that would have
told it a turn was live.

The window is not exotic. The command is `python -m chemclaw.cli.erase_actor <oid> --apply`, run by
a person, against a fleet where that person may well have a tab open.

## Decision

**The sweep takes the durable turn claim on every session it is about to erase, and refuses the run
naming the ones that are busy.** Only the durable claim, because that is the only guard that crosses
a process: this runs from a CLI and the front door's in-process lease is a dict in another pod's
memory. Refusing rather than waiting, because an erasure that silently blocked on a lease would be
indistinguishable from one that hung, and a named session is something an operator can act on. The
claims are released in a `finally` and held under a per-run id, so a late release cannot revoke a
successor's claim — `api/state.TurnLease.token`'s rule, applied here.

**It refuses the dry run too.** That dry run issues the real DELETEs and rolls them back, so it
contends for the same rows; and its whole promise is that its counts are the ones an apply will
produce, which is false while another writer is on the thread. Meeting the refusal at preview time
is also when there is still something to do about it.

**And what the claims cannot cover is counted rather than assumed.** After an applied run, every
session it reached is re-counted and anything that came back lands on `ErasureReport.residue` and in
an ERROR line. This is not belt-and-braces: it is the only cover for the routes a claim has no
reach over — a session created between the enumeration and the commit, a lease that lapsed under a
sweep wider than one lease, and a deployment where nothing takes a durable claim at all
(`api/state._default_turn_claims` returns `None` off the Postgres session store). The count is the
remedy because re-running the erasure is precisely what cannot find the residue. It is derived from
`session_store._session_delete_statements()` rather than listed, since a second hand-written list of
where a session's rows live is this defect one indirection out.

**The same race, one table over, was fixed in one of three sites and is now fixed in all of them.**
`checkpoint_thread_delete_statements` holds the order and the re-ask that
D-2026-09-06-a-sweep-and-a-live-turn-are-two-writers established for the retention sweep;
`leaver._CHECKPOINT_ERASE` and `session_store._session_delete_statements` both build from it.
Reproduced against the shipped statements by stepping them by hand while a second connection played
the checkpointer's write between them — the window is a millisecond wide and cannot be hit by
timing: `residue: 1 checkpoints, 0 blobs`, the surviving row stamping a channel value it can no
longer load.

**Two docstrings about a different pair of records are corrected in the same pass, because they
denied an outcome that happens.** `_record_transcript` and `_roll_back_unfinished` both said the
transcript and the thread cannot disagree across a teardown ("there is no third outcome"). Measured
on a real `run_turn` cancelled between the graph run and the transcript write: `checkpoints: 8,
session_messages: 0` — the model sees the question and the answer, the chemist sees neither, and the
runner logs "the committed turn is kept". The exchange is still kept, which is right; what changes
is that `chemclaw_transcript_thread_divergence_total` says it happened. **The counter names one
branch and not the class**, and says so: the same divergence arrives on the gateway-failure path and
after a cancellation mid-tool, and neither is counted, so a flat series is not proof of agreement.

## Consequences

- `erase_actor` can now fail where it used to succeed, and that is the point. An operator whose
  target has a tab open is told which session and how to clear it.
- `ErasureReport` grows `residue`, and a non-zero one means the erasure is incomplete **and cannot
  be completed by re-running it** — an operator with owner rights has to remove those rows by
  session id.
- A session delete or an erasure that races a live turn now leaves the racing turn's checkpoint
  *with* its blobs, rather than a bricked thread. That is a fuller residue and the right one: it is
  a conversation that still works, and the residue count is what says it is there.
- The two records of one conversation are documented as able to differ by exactly one turn. Closing
  that — projecting the transcript from the checkpoint stream — is the alternative
  `_record_transcript` already names as declined, and the trade was taken on cost before divergence
  was the reason to re-take it. It stays open.
