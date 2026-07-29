# D-130 — Turn teardown runs in a cancelled task, so its cleanup has to be shielded to happen at all

> **On the number.** This was written as D-124 and renumbered on merging second, per `CLAUDE.md`.
> The gap to D-130 is deliberate rather than an accident of the merge. The procedure says "highest
> allocated + 1", which is D-125 — but D-125…D-129 are *intended* by the in-flight storage and
> knowledge-substrate sequence, which reserved them, was forced by `tests/test_decision_log.py` to
> un-reserve them (the registry may not name an ADR that does not exist yet), and still forward-
> references D-125 from the merged body of D-124. Taking D-125 would have followed the letter of a
> rule whose entire purpose is to avoid collisions while causing one, and would have made an
> already-merged ADR's citation wrong. Gaps are explicitly harmless (`CLAUDE.md` rule 4); a
> renumbered neighbour is not.
>
> That contradiction — reserve early, but the test forbids reserving what you have not written — is
> a real defect in the convention, already flagged by the session that hit it (`8f6a319`). It is not
> mine to resolve unilaterally: whoever owns `CLAUDE.md` should decide whether the ledger gains a
> "reserved" state or the advice changes. Recorded here so the next session that trips on it finds
> two witnesses rather than one.

**Context.** Stage 5e's chaos pass found CHAOS-1: abandon an SSE turn mid-stream and the same
session refuses its owner's next turn — `a turn is already running for this session` — for **63
seconds measured**. A chemist who closed a tab could not reopen the conversation for a minute.

The finding stayed open across two sessions because two explanations were tested and **both were
wrong**. Detaching the durable claim release onto its own task changed the measured time not at all
(63.5 s vs 65.1 s). The theory that the abandoned turn simply ran on to completion was refuted by
its actor producing zero `audit_events` rows. The written next step — instrument the teardown — is
what finally settled it, and the instrument mattered more than the reasoning did.

**What it actually was.** Two guards can hold that 409, and no previous measurement separated them.
Sampling both once per second while polling settles it in one run:

```
t+ 0.0s  POST=409  in_flight=0.0  claim=81d518@+59.9s
t+30.9s  POST=409  in_flight=0.0  claim=81d518@+28.9s
t+59.9s  POST=409  in_flight=0.0  claim=81d518@+0.0s
t+60.9s  POST=200  in_flight=0.0  claim=81d518@+60.0s
```

`in_flight` is 0 from the first sample: the in-process `active_turns` set was freed *immediately*,
so the generator's `finally` did run promptly — the third disproved theory. The durable claim's
`expires_at` counts monotonically down from 60 s and is never refreshed, so the heartbeat was
cancelled too. The recovery time is exactly `service_turn_claim_lease_seconds`. **The release never
landed**, and the lease — designed as the backstop — was carrying the whole path.

Tracing the claim store proves the mechanism rather than inferring it:

```
CLAIMTRACE t+ 0.75s claim(71743695) -> True
STREAMTRACE agent stream got CancelledError
CLAIMTRACE t+ 0.75s release(71743695) ENTERED
CLAIMTRACE t+ 0.76s claim(71743695) -> False        <- and no COMPLETED, ever
```

The release is *entered* on every abandoned turn and *completes* on none. sse-starlette answers
`http.disconnect` by cancelling its task group; a bare `await` inside a cancelled task raises at its
first suspension point, so `_release_turn_claim` reached the database call and died there. The
earlier "detach it onto a task" experiment was the right idea measured on the wrong branch.

**Decision.** Shield the release, and give the shielded coroutine its own error handling:

```python
async def _release() -> None:
    try:
        await claims.release(session_id, _WORKER_ID)
    except (ConnectionError, OSError, RuntimeError):
        logger.warning("could not release the turn claim for session %s; it expires on its own", ...)

await asyncio.shield(_release())
```

`shield` runs the release as an independent task that outlives the cancelled frame. The error
handling belongs *inside* that task rather than around the `await`: once the awaiting task is
cancelled, `shield` drops its bookkeeping callback on the inner task, so a failure raised afterwards
is never retrieved and asyncio reports it as a bare `Task exception was never retrieved` with
nothing tying it to a session. A task that cannot fail cannot produce one. The same restructuring is
applied to the runner's pre-existing `rollback_to` shield, which had the identical hazard.

The lease stays as the backstop for what shielding cannot cover — the process being killed, the loop
closing under it. It is now what it was always meant to be: the exceptional path, not the only one.

**The second defect, found by the same instrument.** The trace line `agent stream got
CancelledError` is the answer to a question nobody had asked: which exception does a real disconnect
deliver? `service/runner.py` rolled a half-written turn back under `except GeneratorExit:` — the
exception `aclose()` raises. sse-starlette **never calls `aclose()` on the body iterator** on the
disconnect path; it cancels. So the rollback that exists to stop one dropped connection from
poisoning a conversation with an orphaned `tool_use` was **dead code on the only path that reaches
it**, and had been since it was written. The clause now catches `(GeneratorExit,
asyncio.CancelledError)`, which also brings the front door's whole-turn deadline under the same
rollback — a timed-out turn is half-written in exactly the same way.

This was a silent weakness rather than an outage only because `agents.session_store` repairs
unmatched tool calls at read time. That backstop strips the orphan; only the rollback discards the
rest of the abandoned turn.

**Why no test caught either.** `tests/test_turn_cancellation.py` had three tests about abandoned
turns, every one of them tearing the stream down with `await stream.aclose()` under the comment
*"what sse-starlette does when the client disconnects"*. It is not what sse-starlette does. The
suite simulated the one teardown production never takes, and reported green while the real path was
unhandled — the same shape as LIVE-1, where `ScriptedChatClient` derived from the base class
*without* middleware and so tested a pipeline production never ran.

Writing the regression test reproduced the trap once more, and that is worth recording. The first
version cancelled the consuming task while it sat in its own frame rather than inside the turn; the
abandoned generator was then finalised by `asyncio.run`'s async-generator shutdown, which raises
`GeneratorExit` — so the test passed against the unfixed code. It now waits for the agent to signal
that it has stalled, guaranteeing the cancel lands inside the turn, and asserts *before* the loop
closes.

**Result, measured on the real stack** (live Anthropic, Postgres sessions, disconnect after a
`tool_call` event was seen on the wire):

| | before | after |
|---|---|---|
| single replica: session freed after | **60.9 s** | **0.0 s** |
| two replicas, next turn on the other process | **HTTP 409** | HTTP 200, answered in 4.8 s |
| unmatched `tool_use` ids left in durable history | — | none |

The two-replica row is the one that matters for the shipped chart: a process that never served the
abandoned turn has only the `session_turns` row to go on, so the durable claim is the entire guard
there and its release is the entire fix.

**Cost.** Cleanup now outlives the request that scheduled it, by one task and typically ~140 ms.
That is the price of cleanup that runs at all, and it is bounded: the task does one DELETE and
cannot fail outward.
