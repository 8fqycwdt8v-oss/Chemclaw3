"""A turn that survives its client: detach on disconnect, stop only on request.

**The decision this module carries
(`D-2026-08-27-a-disconnect-is-a-detach-not-a-stop`).** The turn stream used to read any client
disconnect as cancellation, because closing the SSE response was the only way a client could stop
a turn — so the Stop button and a Wi-Fi handoff were indistinguishable, and a 10-minute multi-tool
turn died with the connection that happened to be carrying it. The work was lost from the live
view *and* from the transcript (written only after the answer), on a turn that may have been three
events from delivering it.

So the two meanings are separated. An SSE disconnect — a network blip, a closed laptop, a stalled
reader past the send timeout — **detaches** the client and the turn runs on: the pump task below
keeps driving `run_turn` to completion, the checkpointer and the transcript land exactly as they
would have, and the client recovers the answer from `GET /sessions/{id}/messages` on reconnect.
An explicit **stop** is a first-class request (`POST /sessions/{id}/turn/stop`) that cancels the
pump, which delivers the same `CancelledError` into `run_turn` that a disconnect used to — every
teardown path built for D-130 runs unchanged, in the pump task whose context stamped the ambients.

**The token-budget shape that vetoed stream_events v3 cannot reappear here, by direction.** That
veto was about an abandoned turn booking *less* (v3 booked 0 where the driver books ~30). A
detached turn books *more*: it runs to completion, so every token it spends is metered and billed.
The cost of that honesty is real and stated — a chemist who closes the tab pays for the whole
turn — and it is bounded twice, by the loop cap (attached on every profile) and by
`service_turn_timeout_seconds`, which keeps ticking inside the pump.

**What the pump owns, and the one thing it deliberately does not.** The turn generator's own
`finally` releases the in-process lease and the durable claim; running the generator to completion
in the pump is what keeps both held while the model is genuinely still working — a session stays
409-locked for exactly as long as a turn is running, whether anyone is watching it or not.

The **admission permit** is the exception, and it was not one until it was measured. That permit
is not per session: it is the process's shared `service_max_concurrent_turns` semaphore, so
holding it for a detached turn charges *everyone else on the replica* for work nobody is watching.
Eight fresh sessions POSTed and hung up left 0 of the shipped 8 permits free and shed every other
chemist's turn as `queued` then `error`, for up to `service_turn_timeout_seconds` — reachable by a
flaky mobile network, a crashed tab, or a UI that retries on disconnect, where the retry *adds* a
holder rather than replacing one. Before this module existed a disconnect returned the permit
immediately, so the failure was self-limiting. So the permit is released at the detach, through
`on_detach`: admission is fairness to a *waiting client*, and a detached turn has none. What still
bounds the detached turn is what always did — the loop cap, `service_turn_timeout_seconds` ticking
inside the pump, and the per-user token budget where one is configured — and it stays visible,
because `chemclaw_turns_in_flight` counts leases rather than permits, so a replica running more
turns than it admitted reads as exactly that.

Backpressure survives the detour: while a reader is attached, the pump `put`s into a bounded
queue, so a slow client still slows the turn exactly as the direct generator did. Once the reader
goes, the pump discards instead — an unread event buffered for nobody is memory spent on nobody.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from chemclaw.core.metrics import METRICS

logger = logging.getLogger(__name__)

#: Bounded so a stalled-but-connected client cannot buffer a turn's worth of events in memory —
#: the queue full is what re-creates the direct generator's backpressure. Small, because a healthy
#: client drains far faster than a model produces.
_QUEUE_SIZE = 256

#: End-of-turn marker. Its own object, because `None` could plausibly be an event one day.
_DONE: Any = object()


class DetachableTurn:
    """One running turn, pumped on a task of its own so the response is a view, not the engine.

    `events()` is what the SSE response iterates; cancelling that iteration — a disconnect, a
    send timeout — detaches the reader and nothing else. `stop()` is the only thing that cancels
    the turn itself.
    """

    def __init__(
        self,
        source: AsyncIterator[dict[str, str]],
        *,
        session_id: str,
        survive_disconnect: bool = True,
        on_detach: Callable[[], None] | None = None,
    ) -> None:
        """Start pumping `source` immediately; the turn is running from this moment.

        `survive_disconnect=False` restores the old posture for a deployment that prefers cost
        over completion: a detach then stops the turn, exactly as closing the stream always did.
        The knob lives on the object rather than being read ambiently so a test can pin either
        posture without touching settings.

        `on_detach` fires once, at the instant the reader is known to be gone and the turn is
        known to be continuing — the one moment nothing else in the process can observe. Its
        caller uses it to give back what was held *for the reader* rather than for the turn (see
        `chemclaw.api.routes.turns`); it must not raise and must not block, because it runs inside
        a reader teardown that is usually a cancellation.
        """
        self._session_id = session_id
        self._survive_disconnect = survive_disconnect
        self._on_detach = on_detach
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._attached = True
        self._stopper: asyncio.Task[None] | None = None
        self._task = asyncio.create_task(self._pump(source), name=f"turn:{session_id}")
        # **On the task, not on a reader's path.** See `_note_pump_failure`: the two places a
        # reader could retrieve it are both places a reader may never reach, and a turn that
        # detached has no reader at all. A done callback runs on every ending there is.
        self._task.add_done_callback(self._note_pump_failure)

    @property
    def running(self) -> bool:
        """Whether the turn is still executing — what the stop route answers 404 against."""
        return not self._task.done()

    @property
    def detached(self) -> bool:
        """Whether the client has gone while the turn runs — what `/healthz` style views read."""
        return not self._attached and not self._task.done()

    async def _pump(self, source: AsyncIterator[dict[str, str]]) -> None:
        """Drive the turn to its end, delivering events while anyone is attached.

        The generator's own `finally` — permit, lease, claim, booking — runs here, at the turn's
        *true* end, whichever way it ends. `put_nowait`-then-`put` rather than a bare `put`: the
        attached check and the enqueue must not be separated by an await, or a reader detaching
        between them would leave this parked on a queue nobody drains. A parked `put` can still be
        stranded by a detach landing while it waits; the reader's teardown drains the queue for
        exactly that reason, so the park resolves and the flag takes over.
        """
        try:
            async for item in source:
                if not self._attached:
                    continue
                try:
                    self._queue.put_nowait(item)
                except asyncio.QueueFull:
                    await self._queue.put(item)
        finally:
            self._attached_or_discard(_DONE)

    def _attached_or_discard(self, item: Any) -> None:
        """Deliver `item` if a reader may still come for it, without ever blocking teardown.

        The drop is deliberate and, since `_next_event`, survivable. Teardown must not block —
        a `finally` that awaits a full queue would park the turn's own cleanup behind a reader —
        so a full queue loses whatever is offered here, `_DONE` included. That loss used to end
        the stream: the reader was parked on `queue.get()` with the pump already finished and
        nothing left to wake it. `_next_event` reads the pump's *state* rather than only its
        marker, so the marker is the ordinary terminator rather than the only one.
        """
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(item)

    async def _next_event(self) -> Any:
        """The next queued event, or `_DONE` once the turn is over — marker or no marker.

        **The bug this closes was a live hang, and the trigger is an ordinary turn.** `_pump`
        blocks on `await put` when the queue fills, so the moment its last blocking put returns
        the queue is full again — and the `finally` one line later offers `_DONE` through
        `put_nowait`, which is dropped. Reproduced at `_QUEUE_SIZE` and `2 * _QUEUE_SIZE` events
        with a reader momentarily behind (a token-streamed answer to a slightly slow client):
        the pump task finished, the queue drained to empty, and `events()` awaited a marker that
        no longer existed. Nothing sends on that connection, so the SSE send timeout never fires
        and the 15 s ping keeps succeeding; the stream stays open for the pod's lifetime holding
        a slot against `--limit-concurrency`.

        So end-of-stream is decided by the fact rather than by the message: the pump task being
        done, with the queue drained, *is* the end of the turn. The marker is kept because it is
        how nearly every stream actually ends — it reaches the reader through the getter below,
        not through the queue-non-empty branch, which the comment there measures — and racing the
        task is what makes the exception terminate. `asyncio.wait` rather than `wait_for`, because
        there is no timeout here to pick — the two things that can happen are an event arriving
        and the turn ending.
        """
        # **The queue-non-empty path is the *rare* one, and this comment used to claim the
        # opposite** ("every event of a healthy stream"). A healthy stream is one whose reader
        # outruns its producer, so the queue is empty at nearly every read. Measured over 101
        # reads, with the producer pausing 1 ms between tokens — slower than that is what a real
        # provider does: this branch ran **once** and the task-juggling path below ran **100**
        # times. Only a synthetic burst with no await at all inverts it (67 against 34), which is
        # the shape the old comment was written from.
        #
        # So the per-event cost is real and is paid on nearly every event: an `ensure_future` plus
        # an `asyncio.wait` measured at ~22 µs against ~0.4 µs for a bare `await queue.get()`.
        # It stays, and what it buys is stated rather than assumed. Removing the getter task means
        # the marker must be *guaranteed*, and the only way to guarantee a `put_nowait` into a
        # full queue is to drop an event to make room — trading a hang that is now fixed for a
        # hole in the stream the chemist is reading. A reserved marker slot (a semaphore of
        # `_QUEUE_SIZE` permits over a queue of `_QUEUE_SIZE + 1`) would buy it honestly, and is a
        # second synchronisation primitive in a module whose last two defects were both races —
        # not worth 20 µs on a path whose events arrive a millisecond apart.
        if not self._queue.empty():
            return self._queue.get_nowait()
        if self._task.done():
            return _DONE
        getter = asyncio.ensure_future(self._queue.get())
        try:
            await asyncio.wait({getter, self._task}, return_when=asyncio.FIRST_COMPLETED)
            if getter.done():
                return getter.result()
        finally:
            # Including on the reader's own cancellation, which is the detach path: an orphaned
            # getter would otherwise outlive the stream it was reading for. Cancelling a woken
            # `Queue.get` does not consume the item — asyncio re-wakes the next getter and leaves
            # it queued — so the drain below still sees everything the pump delivered.
            if not getter.done():
                getter.cancel()
        return self._queue.get_nowait() if not self._queue.empty() else _DONE

    def _note_pump_failure(self, task: "asyncio.Task[None]") -> None:
        """Log a pump that ended by raising, and *retrieve* it so asyncio does not shout at GC.

        `run_turn` turns every `Exception` into an error event, so a failure reaching here was
        above it — and until this ran as a done callback, nothing retrieved it. Measured across
        eight raise scenarios: **0 calls, 0 log records, and `task._log_traceback is True` in all
        eight**, which is asyncio's flag for "I will print `Task exception was never retrieved` at
        garbage-collection time" — under no session, no correlation id, and possibly never. That
        is the exact outcome the docstring said this prevented.

        The reason it never ran is that it was called on one branch of `_next_event`, and a raising
        pump does not reach that branch — in any of the eight, `_QUEUE_SIZE` and past it included:
        `_pump`'s own `finally` offers `_DONE` first, so the parked getter wakes with the marker
        and returns one line earlier. The two reader-side
        placements the review offered (that branch, or `events()`'s `finally`) share a deeper
        problem anyway — a detached turn has no reader, and a reader that is cancelled mid-stream
        never runs another line of this class. A done callback is the one hook that fires on every
        ending, exactly once, whether anybody was watching or not.

        `CancelledError` is excluded because it is the ordinary stop path.
        """
        if task.cancelled():
            return
        # Called for its side effect as much as its value: retrieving the exception is what clears
        # asyncio's `_log_traceback`, and it must happen even when there is nothing to log.
        failure = task.exception()
        if failure is not None:
            logger.warning(
                "the turn pump for session %s ended by raising; the stream is closed",
                self._session_id,
                exc_info=failure,
            )

    async def events(self) -> AsyncIterator[dict[str, str]]:
        """The reader's view of the turn. Cancelling it detaches; the turn does not notice.

        The `finally` marks the reader gone and drains whatever is queued, which is also what
        releases a pump parked on a full queue (see `_pump`). After it runs, the pump discards.
        """
        try:
            while True:
                item = await self._next_event()
                if item is _DONE:
                    return
                yield item
        finally:
            if self.running and self._attached:
                if self._survive_disconnect:
                    METRICS.increment("chemclaw_turns_detached_total")
                    logger.info(
                        "the client of session %s went away mid-turn; the turn continues "
                        "detached and its answer will be in the transcript",
                        self._session_id,
                    )
                    if self._on_detach is not None:
                        self._on_detach()
                else:
                    # The configured posture is the old one: a disconnect stops the turn. On a
                    # task because this finally runs inside the reader's own cancellation, where
                    # an await re-raises immediately; held on the instance so the write cannot be
                    # garbage-collected mid-cancel.
                    self._stopper = asyncio.get_running_loop().create_task(self.stop())
            self._attached = False
            while not self._queue.empty():
                self._queue.get_nowait()

    async def stop(self) -> None:
        """Cancel the running turn — the explicit act a disconnect no longer performs.

        The cancellation lands inside `run_turn` exactly where a disconnect used to land it, so
        the whole D-130 teardown — rollback, booking, ambient resets, the released permit — runs
        unchanged. Awaited so the caller's 200 means "stopped", not "asked nicely".

        **The `Exception` arm says so out loud now, and used to say nothing at all.** A cancelled
        turn ending in `CancelledError` is the expected outcome and stays quiet; anything else is
        a teardown that failed — a rollback that raised, a booking that raised — and swallowing it
        left the stop route answering 200 with the only record of the failure discarded. Still
        suppressed, because the turn *is* stopped either way and the caller's answer is the same;
        logged, because "stopped cleanly" and "stopped, and its teardown broke" are different
        facts and only the server can keep the second one.
        """
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            # **Whose cancellation was that?** `await self._task` raises the same `CancelledError`
            # for "the turn I just cancelled ended" and for "the stop route's own handler was
            # cancelled while waiting" — a client that gave up on the stop request, a pod draining
            # — and swallowing the second is swallowing a cancellation addressed to this frame,
            # which asyncio requires to propagate. The task's own state tells them apart: it is
            # `cancelled()` only in the first case, and merely not-done in the second.
            if not self._task.cancelled():
                raise
        except Exception:
            logger.warning(
                "session %s's turn was stopped and its teardown raised; the turn is cancelled "
                "either way",
                self._session_id,
                exc_info=True,
            )


class RunningTurns:
    """The per-process registry the stop route resolves a session's live turn from.

    A thin dict wrapper rather than a bare dict on `app.state`, so registration and expiry are
    written once: an entry is removed when its task finishes, whichever way, via the done
    callback — there is no path that leaves a dead turn answering `running`.
    """

    def __init__(self) -> None:
        """Start empty; turns register themselves via `register`."""
        self._turns: dict[str, DetachableTurn] = {}

    def register(self, session_id: str, turn: DetachableTurn) -> None:
        """Track `turn` as the session's running turn until its pump finishes."""
        self._turns[session_id] = turn
        turn._task.add_done_callback(lambda _t: self._forget(session_id, turn))

    def _forget(self, session_id: str, turn: DetachableTurn) -> None:
        """Drop the entry, identity-checked so a successor's registration is never revoked."""
        if self._turns.get(session_id) is turn:
            del self._turns[session_id]

    def get(self, session_id: str) -> DetachableTurn | None:
        """The session's running turn, or `None` when no turn is live."""
        turn = self._turns.get(session_id)
        return turn if turn is not None and turn.running else None

    async def drain(self, timeout: float) -> int:
        """Wait up to `timeout` for every live pump to finish; report how many did not.

        **This is what makes a detached turn survive a rolling update rather than only a
        disconnect.** A pump task is not an in-flight HTTP request, so uvicorn's own drain does not
        know one exists; without this the front door's lifespan `finally` closed the memory store,
        the checkpointer's pool and the shared store pool while turns were still running, and the
        answer this module exists to deliver was lost from the transcript it promised to be in.
        Measured before it existed: shutdown returned in 0.001 s and the running turn's next
        checkpoint write raised `PoolClosed`.

        The registry already holds every live turn and already prunes on completion, so this is a
        snapshot plus one `asyncio.wait`. Snapshot, because `register`'s done callback deletes from
        the same dict as each pump finishes.

        Nothing is cancelled here. A turn is bounded by its own `service_turn_timeout_seconds`
        deadline, measured from when *it* started, so a caller passing that same number can only
        be reached by a turn whose deadline is already firing — and cutting a turn short to save a
        second of a grace period the chart has already provisioned would trade the answer for
        nothing. What is left running is *said*, because a pod that exits with work in flight is a
        fact an operator has to be able to find.
        """
        pumps = [turn._task for turn in list(self._turns.values()) if not turn._task.done()]
        if not pumps:
            return 0
        logger.info("draining %d running turn(s) before shutdown", len(pumps))
        _finished, pending = await asyncio.wait(pumps, timeout=timeout)
        if pending:
            logger.warning(
                "%d of %d running turn(s) did not finish within the %ss shutdown drain; their "
                "answers will not reach the transcript",
                len(pending),
                len(pumps),
                timeout,
            )
        return len(pending)
