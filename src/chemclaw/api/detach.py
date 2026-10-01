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
claimed — and the next message in its line waiting — for exactly as long as a turn is running,
whether anyone is watching it or not.

The **admission permit** is the exception, and it was not one until it was measured. That permit
is not per session: it is the process's shared `service_max_concurrent_turns` semaphore, so
holding it for a detached turn charges *everyone else on the replica* for work nobody is watching.
Measured by POSTing and hanging up one fresh session per permit: every permit on the replica was
held, and every other chemist's turn was shed as `queued` then `error`, for up to
`service_turn_timeout_seconds` — reachable by a flaky mobile network, a crashed tab, or a UI that
retries on disconnect, where the retry *adds* a holder rather than replacing one. The cap is a
setting (`core/config/service.py`) and this paragraph names no value for it on purpose: the
measurement is "one hung-up client per permit", which holds at whatever the cap is, and the number
this sentence used to carry was 8 while the shipped default had moved to 12. Before this module
existed a disconnect returned the permit immediately, so the failure was self-limiting. So the
permit is released at the detach, through
`on_detach`: admission is fairness to a *waiting client*, and a detached turn has none. What still
bounds the detached turn is what always did — the loop cap, `service_turn_timeout_seconds` ticking
inside the pump, and the per-user token budget where one is configured — and it stays visible,
because `chemclaw_turns_in_flight` counts leases rather than permits, so a replica running more
turns than it admitted reads as exactly that.

**Several people can watch one turn, and none of them can hold it**
(`D-2026-10-01-a-queued-message-waits-in-its-senders-request`). A shared session has more than one
participant, so a turn has more than one reader: the sender's own stream, and any other
participant's `GET /sessions/{id}/turn/stream`. Each reader has **its own** bounded buffer and the
pump never waits on any of them — it offers every event to every attached reader and moves on.
A reader whose buffer is full has stopped reading, and it is cut off with one `stream_lagged`
error rather than allowed to slow the turn or anybody else's view. That is a change for the sender
too, and a deliberate one: a single reader used to push back on the pump through a shared queue,
so a stalled browser held the turn until the send timeout closed it. Measured, a healthy reader
outruns the producer (the empty-queue path ran on 100 of 101 reads at a 1 ms token cadence), so the
buffer is only ever filled by a reader that has stopped — and the thing to protect from it is the
turn, not the reader. A late joiner sees the turn from the moment it attaches: the stream carries
no event ids, so there is nothing to replay from, and what came before is in the transcript once
the answer lands — which is exactly what a sender reconnecting after a detach gets.

The **sender's** reader keeps the one role no watcher has: its departure is the detach
(`on_detach`, or a stop under `survive_disconnect=False`). A watcher closing their tab changes
nothing about the turn.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Any

from chemclaw.api.events import ErrorEvent, sse_frame
from chemclaw.core.metrics import METRICS

logger = logging.getLogger(__name__)

#: Per reader, so a stalled browser can fill only its own. Sized well past anything a *reading*
#: client accumulates — a healthy reader finds its buffer empty on nearly every read — because the
#: only reader that reaches it is one that has stopped, and cutting that one off is the point. It
#: also bounds the memory one abandoned view can pin for as long as it takes the send timeout to
#: close it.
_QUEUE_SIZE = 1024

#: End-of-turn marker. Its own object, because `None` could plausibly be an event one day.
_DONE: Any = object()

#: This reader fell a full buffer behind and the pump stopped offering it events.
_LAGGED: Any = object()

#: What a cut-off reader is told. An error event because the stream contract is that a stream ends
#: with an answer or an error, and a view that simply stopped would read as a turn that did.
_LAGGED_MESSAGE = (
    "This view of the turn fell too far behind and was closed; the turn is still running. Reopen "
    "it, or read the answer in the conversation once it lands."
)


class _Reader:
    """One view of a running turn: its own bounded buffer, and whether the pump has cut it off."""

    __slots__ = ("queue", "lagged")

    def __init__(self) -> None:
        """An empty buffer, attached."""
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self.lagged = False


class DetachableTurn:
    """One running turn, pumped on a task of its own so each response is a view, not the engine.

    `events()` is the sender's view and `watch()` any other participant's; cancelling either — a
    disconnect, a send timeout — detaches that reader and nothing else. `stop()` is the only thing
    that cancels the turn itself.
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

        The sender's reader is attached here rather than when `events()` is first iterated, so
        nothing the turn produces before the response starts is lost.

        `survive_disconnect=False` restores the old posture for a deployment that prefers cost
        over completion: the sender's detach then stops the turn, exactly as closing the stream
        always did. The knob lives on the object rather than being read ambiently so a test can pin
        either posture without touching settings.

        `on_detach` fires once, at the instant the sender's reader is known to be gone and the
        turn is known to be continuing — the one moment nothing else in the process can observe.
        Its caller uses it to give back what was held *for the reader* rather than for the turn
        (see `chemclaw.api.routes.turns`); it must not raise and must not block, because it runs
        inside a reader teardown that is usually a cancellation.
        """
        self._session_id = session_id
        self._survive_disconnect = survive_disconnect
        self._on_detach = on_detach
        self._sender = _Reader()
        self._readers: set[_Reader] = {self._sender}
        self._sender_attached = True
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
        """Whether the sender has gone while the turn runs — watchers do not count."""
        return not self._sender_attached and not self._task.done()

    @property
    def watchers(self) -> int:
        """How many participants besides the sender are following the turn right now."""
        return len(self._readers - {self._sender})

    async def _pump(self, source: AsyncIterator[dict[str, str]]) -> None:
        """Drive the turn to its end, offering each event to every reader still attached.

        The generator's own `finally` — permit, lease, claim, booking — runs here, at the turn's
        *true* end, whichever way it ends. Nothing here awaits a reader: see `_offer`.
        """
        try:
            async for item in source:
                self._offer(item)
        finally:
            # Never blocking teardown: a full buffer loses the marker, and `_next_event` reads the
            # pump's *state* for exactly that case, so the marker is the ordinary terminator rather
            # than the only one.
            for reader in list(self._readers):
                with contextlib.suppress(asyncio.QueueFull):
                    reader.queue.put_nowait(_DONE)

    def _offer(self, item: Any) -> None:
        """Put `item` in every attached reader's buffer; cut off any reader whose buffer is full.

        No `await`, so a reader cannot detach half-way through one delivery and no reader's pace
        reaches the turn. A cut-off reader keeps what is already buffered — it reads that first and
        is then told it lagged — so it never sees a gap in the middle of its stream, only an end.
        """
        for reader in list(self._readers):
            try:
                reader.queue.put_nowait(item)
            except asyncio.QueueFull:
                reader.lagged = True
                self._readers.discard(reader)
                METRICS.increment("chemclaw_turn_readers_lagged_total")
                logger.info(
                    "a reader of session %s's turn fell %d events behind and was cut off; the turn "
                    "continues",
                    self._session_id,
                    _QUEUE_SIZE,
                )

    async def _next_event(self, reader: _Reader) -> Any:
        """The reader's next event, `_LAGGED` once it was cut off, or `_DONE` once the turn is over.

        **The bug this closes was a live hang, and the trigger is an ordinary turn.** The pump's
        `finally` offers `_DONE` with `put_nowait`, and a buffer that is full at that moment drops
        it. Reproduced (when the pump still blocked on one shared queue) at the queue's size and
        twice it with a reader momentarily behind: the pump task finished, the queue drained to
        empty, and the reader awaited a marker that no longer existed. Nothing sends on that
        connection, so the SSE send timeout never fires and the 15 s ping keeps succeeding; the
        stream stays open for the pod's lifetime holding a slot against `--limit-concurrency`.

        So end-of-stream is decided by the fact rather than by the message: the pump task being
        done, with the buffer drained, *is* the end of the turn. `asyncio.wait` rather than
        `wait_for`, because there is no timeout here to pick — the two things that can happen are
        an event arriving and the turn ending.

        **Lagged is checked before done**, because a reader that was cut off missed events and
        must be told so even if the turn has since finished — "the turn ended" would be a false
        account of a stream with a hole in its tail.
        """
        # **The buffer-non-empty path is the *rare* one.** A healthy stream is one whose reader
        # outruns its producer, so the buffer is empty at nearly every read. Measured over 101
        # reads, with the producer pausing 1 ms between tokens — slower than that is what a real
        # provider does: this branch ran **once** and the task-juggling path below ran **100**
        # times. The per-event cost of that path (~22 µs against ~0.4 µs for a bare `get`) stays,
        # because removing the getter task means the marker must be *guaranteed*, which costs a
        # second synchronisation primitive in a module whose defects have all been races.
        if not reader.queue.empty():
            return reader.queue.get_nowait()
        if reader.lagged:
            return _LAGGED
        if self._task.done():
            return _DONE
        getter = asyncio.ensure_future(reader.queue.get())
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
        return reader.queue.get_nowait() if not reader.queue.empty() else _DONE

    def _note_pump_failure(self, task: "asyncio.Task[None]") -> None:
        """Log a pump that ended by raising, and *retrieve* it so asyncio does not shout at GC.

        `run_turn` turns every `Exception` into an error event, so a failure reaching here was
        above it — and until this ran as a done callback, nothing retrieved it. Measured across
        eight raise scenarios: **0 calls, 0 log records, and `task._log_traceback is True` in all
        eight**, which is asyncio's flag for "I will print `Task exception was never retrieved` at
        garbage-collection time" — under no session, no correlation id, and possibly never.

        A reader-side placement shares a deeper problem — a detached turn has no reader, and a
        reader that is cancelled mid-stream never runs another line of this class. A done callback
        is the one hook that fires on every ending, exactly once, whether anybody was watching or
        not.

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

    def events(self) -> AsyncIterator[dict[str, str]]:
        """The sender's view of the turn. Cancelling it detaches; the turn does not notice."""
        return self._view(self._sender)

    def watch(self) -> AsyncGenerator[dict[str, str], None] | None:
        """Another participant's view, from this moment on; `None` once the turn is over.

        Attached here, synchronously, rather than on first iteration, so the view starts at the
        event after this call and not at whatever the response's first read happens to be.
        Whether this caller may watch — a participant of *this* session, within the watcher cap —
        is the route's to decide before it calls; this object is reached only through the session
        it was registered under.
        """
        if not self.running:
            return None
        reader = _Reader()
        self._readers.add(reader)
        return self._view(reader)

    async def _view(self, reader: _Reader) -> AsyncGenerator[dict[str, str], None]:
        """One reader's stream, to the turn's end, its own cut-off, or its own cancellation.

        The `finally` detaches the reader and drains its buffer. For the sender's reader it is also
        the detach itself: the turn continues (and `on_detach` gives back what was held for the
        reader), or — under the old posture — stops.
        """
        try:
            while True:
                item = await self._next_event(reader)
                if item is _DONE:
                    return
                if item is _LAGGED:
                    yield sse_frame(
                        ErrorEvent(message=_LAGGED_MESSAGE, code="stream_lagged", retryable=True)
                    )
                    return
                yield item
        finally:
            self._readers.discard(reader)
            if reader is self._sender:
                self._sender_gone()
            while not reader.queue.empty():
                reader.queue.get_nowait()

    def _sender_gone(self) -> None:
        """The sender's reader has left: detach (and give back its hold), or stop, once."""
        if self.running and self._sender_attached:
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
                # task because this runs inside the reader's own cancellation, where an await
                # re-raises immediately; held on the instance so the write cannot be
                # garbage-collected mid-cancel.
                self._stopper = asyncio.get_running_loop().create_task(self.stop())
        self._sender_attached = False

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
