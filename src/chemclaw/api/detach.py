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

**What the pump owns.** The turn generator's own `finally` releases the admission permit, the
in-process lease and the durable claim; running the generator to completion in the pump is what
keeps all three held while the model is genuinely still working — a session stays 409-locked for
exactly as long as a turn is running, whether anyone is watching it or not.

Backpressure survives the detour: while a reader is attached, the pump `put`s into a bounded
queue, so a slow client still slows the turn exactly as the direct generator did. Once the reader
goes, the pump discards instead — an unread event buffered for nobody is memory spent on nobody.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
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
    ) -> None:
        """Start pumping `source` immediately; the turn is running from this moment.

        `survive_disconnect=False` restores the old posture for a deployment that prefers cost
        over completion: a detach then stops the turn, exactly as closing the stream always did.
        The knob lives on the object rather than being read ambiently so a test can pin either
        posture without touching settings.
        """
        self._session_id = session_id
        self._survive_disconnect = survive_disconnect
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._attached = True
        self._stopper: asyncio.Task[None] | None = None
        self._task = asyncio.create_task(self._pump(source), name=f"turn:{session_id}")

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
        marker, so the marker is now the fast path rather than the only path.
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
        the common case and costs one comparison; racing the task is what makes the uncommon one
        terminate. `asyncio.wait` rather than `wait_for`, because there is no timeout here to
        pick — the two things that can happen are an event arriving and the turn ending.
        """
        # Fast path first: with something already queued there is no task juggling to do, which
        # is every event of a healthy stream.
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
        self._note_pump_failure()
        return self._queue.get_nowait() if not self._queue.empty() else _DONE

    def _note_pump_failure(self) -> None:
        """Log a pump that ended by raising, which would otherwise be retrieved by nobody.

        `run_turn` turns every `Exception` into an error event, so reaching here means the failure
        was above it — and the task's exception is never retrieved, so asyncio reports it at
        garbage-collection time under no session and no correlation id, or not at all.
        `CancelledError` is excluded because it is the ordinary stop path.
        """
        if self._task.cancelled():
            return
        failure = self._task.exception()
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
            pass
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
