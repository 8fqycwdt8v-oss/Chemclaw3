"""A turn that survives its client: detach on disconnect, stop only on request.

(`D-2026-08-27-a-disconnect-is-a-detach-not-a-stop`.) An SSE disconnect detaches the client and the
turn runs on: a pump task drives `run_turn` to completion, the checkpoint and transcript land as
usual, and the client recovers the answer on reconnect. An explicit stop (`POST
/sessions/{id}/turn/stop`) cancels the pump, delivering `CancelledError` into `run_turn` so every
existing teardown path runs in the task whose context stamped the ambients. A detached turn is
billed in full, bounded by the loop cap and `service_turn_timeout_seconds`.

The generator's `finally` releases the lease and the durable claim, so a session stays claimed for
exactly as long as its turn runs. The admission permit is the exception: it is the process-wide
semaphore, so it is released at the detach (`on_detach`); otherwise hung-up clients could hold
every permit on the replica. `chemclaw_turns_in_flight` counts leases, so detached turns stay
visible.

Several participants can watch one turn. Each reader has its own bounded buffer and the pump never
waits on one: a reader whose buffer fills is cut off with one `stream_lagged` error. A late joiner
sees events from attach onward (there are no event ids to replay from). Only the sender's departure
is a detach; a watcher closing their tab changes nothing. A watch counts against
`service_turn_max_watchers` until its response ends (`Watch.close`), not merely until it is cut off.

A stop from an unloading page is deferred (`defer_stop`) for `service_turn_unload_grace_seconds`,
since a reload and a close look the same; the sender returning cancels it (`resume`). An explicit
stop is immediate.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Any, NamedTuple

from chemclaw.api.events import ErrorEvent, sse_frame
from chemclaw.core.metrics import METRICS

logger = logging.getLogger(__name__)

#: Per reader, so a stalled browser fills only its own. Large, because only a reader that has
#: stopped fills it; it also bounds what one abandoned view pins until the send timeout closes it.
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


def lagged_frame() -> dict[str, str]:
    """The frame a view that fell a full buffer behind ends on — here and when relayed elsewhere."""
    return sse_frame(ErrorEvent(message=_LAGGED_MESSAGE, code="stream_lagged", retryable=True))


class _DraftSlot:
    """A queued `exhibit_draft` frame whose content a newer frame of the same call may replace.

    Each draft frame carries the whole document so far, so an untaken frame is worthless once a
    newer one exists; a stalled reader would otherwise pin a buffer full of large documents. A
    buffer holds at most one draft per call, refreshed in place. Every other event is queued as is.
    """

    __slots__ = ("call_id", "frame")

    def __init__(self, call_id: str, frame: dict[str, str]) -> None:
        """Hold `frame` for `call_id` until the reader takes it."""
        self.call_id = call_id
        self.frame = frame


class _Reader:
    """One view of a running turn: its own bounded buffer, and whether the pump has cut it off."""

    __slots__ = ("queue", "lagged", "drafts")

    def __init__(self) -> None:
        """An empty buffer, attached."""
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self.lagged = False
        # The draft slots still in `queue`, by tool-call id — what `_offer` refreshes in place.
        self.drafts: dict[str, _DraftSlot] = {}


def _draft_call_id(item: Any) -> str | None:
    """The tool-call id of an `exhibit_draft` frame, or `None` for every other item.

    Read off the frame's JSON, parsed only for the coalesced event type.
    """
    if not isinstance(item, dict) or item.get("event") != "exhibit_draft":
        return None
    try:
        call_id = json.loads(item["data"]).get("call_id")
    except (KeyError, TypeError, ValueError):
        return None
    return call_id if isinstance(call_id, str) else None


class Watch(NamedTuple):
    """One participant's view of a running turn, and the release of its place under the cap."""

    #: The view itself, from the moment `watch()` attached it.
    events: AsyncGenerator[dict[str, str], None]
    #: Give the watcher's place back. Idempotent; the response serving `events` calls it when it
    #: ends, which also covers a view whose generator never started and so runs no `finally`.
    close: Callable[[], None]


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
        correlation_id: str = "",
    ) -> None:
        """Start pumping `source` immediately; the turn is running from this moment.

        The sender's reader is attached here, so nothing produced before the response starts is
        lost.

        `survive_disconnect=False` makes the sender's detach stop the turn (cost over completion);
        held on the object so tests can pin either posture. `on_detach` fires once, when the
        sender's reader is gone and the turn continues, to give back what was held for the reader
        (see `chemclaw.api.routes.turns`); it runs inside a cancellation and must not raise or
        block. `correlation_id` is the starting request's, so a reattaching page can tell its turn
        from a later one (`TURN_CORRELATION_HEADER`).
        """
        self._session_id = session_id
        self.correlation_id = correlation_id
        self._survive_disconnect = survive_disconnect
        self._on_detach = on_detach
        self._sender = _Reader()
        self._readers: set[_Reader] = {self._sender}
        # Watchers whose stream is still open — fed or cut off — which is what the cap counts.
        self._watching: set[_Reader] = set()
        # Who each open watch belongs to, so a deferred unload stop can tell whether the page it
        # was waiting for is already here (`_stop_after`).
        self._watcher_oids: dict[_Reader, str | None] = {}
        self._sender_attached = True
        self._stopper: asyncio.Task[None] | None = None
        # An unload stop waiting out its grace window (`defer_stop`), who may cancel it by
        # reattaching, and how many this turn has been granted.
        self._pending_stop: asyncio.Task[None] | None = None
        self._resumers: frozenset[str] = frozenset()
        self._deferrals = 0
        self._task = asyncio.create_task(self._pump(source), name=f"turn:{session_id}")
        # A done callback, because a detached turn has no reader to retrieve the exception.
        self._task.add_done_callback(self._note_pump_failure)
        # A turn that ends inside a grace window takes its pending stop with it.
        self._task.add_done_callback(lambda _t: self._drop_pending_stop())

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
        """How many participants besides the sender hold a view of the turn open right now.

        A cut-off view still counts until its stream closes, since its socket and buffer are still
        held.
        """
        return len(self._watching)

    async def _pump(self, source: AsyncIterator[dict[str, str]]) -> None:
        """Drive the turn to its end, offering each event to every reader still attached.

        The generator's own `finally` (permit, lease, claim, booking) runs here at the turn's true
        end. Never awaits a reader: see `_offer`.
        """
        try:
            async for item in source:
                self._offer(item)
        finally:
            # Never block teardown: a full buffer loses the marker, and `_next_event` checks the
            # pump's state for that case.
            for reader in list(self._readers):
                with contextlib.suppress(asyncio.QueueFull):
                    reader.queue.put_nowait(_DONE)

    def _offer(self, item: Any) -> None:
        """Put `item` in every attached reader's buffer; cut off any reader whose buffer is full.

        No `await`, so no reader's pace reaches the turn. A cut-off reader reads what it already
        has, then learns it lagged — an end, never a gap. A draft frame replaces an older waiting
        one of its call (`_DraftSlot`).
        """
        call_id = _draft_call_id(item)
        for reader in list(self._readers):
            if call_id is not None and (slot := reader.drafts.get(call_id)) is not None:
                slot.frame = item
                continue
            queued = item if call_id is None else _DraftSlot(call_id, item)
            try:
                reader.queue.put_nowait(queued)
                if isinstance(queued, _DraftSlot):
                    reader.drafts[queued.call_id] = queued
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

        End-of-stream is decided by the pump task being done with the buffer drained, not by the
        `_DONE` marker, which a full buffer can drop — otherwise the reader would wait forever on a
        connection the ping keeps alive. Lagged is checked before done: a cut-off reader missed
        events and must be told.
        """
        # Fast path when events are buffered (rare: a healthy reader outruns the producer). The
        # slower getter-task path below is kept because it guarantees the end is seen without a
        # second synchronisation primitive.
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
            # Including on the reader's own cancellation (the detach path). Cancelling a woken
            # `Queue.get` does not consume the item, so the drain still sees everything.
            if not getter.done():
                getter.cancel()
        return reader.queue.get_nowait() if not reader.queue.empty() else _DONE

    def _note_pump_failure(self, task: "asyncio.Task[None]") -> None:
        """Log a pump that ended by raising, and retrieve its exception so asyncio stays quiet.

        `run_turn` turns every `Exception` into an error event, so anything reaching here was above
        it. A done callback is the one hook that fires on every ending, reader or not.
        `CancelledError` is the ordinary stop path and is excluded.
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

    def watch(self, oid: str | None = None) -> Watch | None:
        """Another participant's view, from this moment on; `None` once the turn is over.

        `oid` is whose view it is, held while open: a deferred unload stop expiring while its sender
        watches means the reload arrived first. Attached synchronously so the view starts at the
        next event. Whether the caller may watch is the route's decision.
        """
        if not self.running:
            return None
        reader = _Reader()
        self._readers.add(reader)
        self._watching.add(reader)
        self._watcher_oids[reader] = oid

        def _close() -> None:
            """Release the view's place, whether or not its generator ever ran."""
            self._readers.discard(reader)
            self._watching.discard(reader)
            self._watcher_oids.pop(reader, None)

        return Watch(self._view(reader), _close)

    async def _view(self, reader: _Reader) -> AsyncGenerator[dict[str, str], None]:
        """One reader's stream, to the turn's end, its own cut-off, or its own cancellation.

        The `finally` detaches the reader and drains its buffer. For the sender's reader it is also
        the detach: the turn continues (and `on_detach` runs), or, under `survive_disconnect=False`,
        stops.
        """
        try:
            while True:
                item = await self._next_event(reader)
                if item is _DONE:
                    return
                if item is _LAGGED:
                    yield lagged_frame()
                    return
                if isinstance(item, _DraftSlot):
                    # Taken: a later draft of this call is queued afresh, behind what came between.
                    reader.drafts.pop(item.call_id, None)
                    item = item.frame
                yield item
        finally:
            self._readers.discard(reader)
            self._watching.discard(reader)
            self._watcher_oids.pop(reader, None)
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
                # Configured to stop on disconnect. On a task because an await here would re-raise
                # the cancellation immediately; held on the instance so it is not garbage-collected.
                self._stopper = asyncio.get_running_loop().create_task(self.stop())
        self._sender_attached = False

    @property
    def stop_pending(self) -> bool:
        """Whether an unload stop is waiting out its grace window on this turn."""
        return self._pending_stop is not None

    def defer_stop(self, grace: float, *, resumers: frozenset[str], max_deferrals: int) -> bool:
        """Stop the turn in `grace` seconds unless one of `resumers` reattaches first.

        A discarded page stops its turn, but at unload a reload looks like a close, so the stop
        waits.

        `True` when a window is pending after this call — new or already pending, whose deadline is
        not moved, so repeating the stop cannot keep a turn alive. `False` when no window may be
        granted (turn over, or `max_deferrals` used, bounding a reload loop); the caller then stops
        at once. `resumers` are the principal who asked and the turn's sender.
        """
        if not self.running:
            return False
        if self._pending_stop is not None:
            return True
        if self._deferrals >= max_deferrals:
            return False
        self._deferrals += 1
        self._resumers = resumers
        self._pending_stop = asyncio.get_running_loop().create_task(
            self._stop_after(grace), name=f"turn-unload-stop:{self._session_id}"
        )
        return True

    def resume(self, oid: str | None) -> bool:
        """Cancel a pending unload stop because `oid` came back to the turn; whether it did.

        Only a principal named when the stop was deferred can cancel it.
        """
        if self._pending_stop is None or oid is None or oid not in self._resumers:
            return False
        self._drop_pending_stop()
        METRICS.increment("chemclaw_turns_stop_resumed_total")
        logger.info(
            "session %s's turn was reattached inside its unload grace; the deferred stop is "
            "cancelled and the turn continues",
            self._session_id,
        )
        return True

    def _drop_pending_stop(self) -> None:
        """Cancel the pending stop's timer, if there is one; idempotent."""
        pending, self._pending_stop = self._pending_stop, None
        if pending is not None and not pending.done():
            pending.cancel()

    async def _stop_after(self, grace: float) -> None:
        """Wait out the grace window, then stop the turn — unless whoever it waited for is here.

        Checked at expiry, not when the stop arrives: the reloaded page's watch may arrive before
        the old page's stop, and an unloading page's own watch closes within the window.
        """
        await asyncio.sleep(grace)
        # Past the window the stop is no longer cancellable: released *before* the await below, so
        # a reattach racing it cannot cancel this task half-way through the turn's teardown.
        self._pending_stop = None
        if not self.running:
            return
        if any(oid in self._resumers for oid in self._watcher_oids.values() if oid):
            METRICS.increment("chemclaw_turns_stop_resumed_total")
            logger.info(
                "session %s's deferred unload stop expired with its sender watching (a reload that "
                "arrived before the stop); the turn continues",
                self._session_id,
            )
            return
        METRICS.increment("chemclaw_turns_stop_expired_total")
        METRICS.increment("chemclaw_turns_stopped_total")
        logger.info(
            "session %s's turn was stopped: its page unloaded and nobody reattached within %ss",
            self._session_id,
            grace,
        )
        await self.stop()

    async def stop(self) -> None:
        """Cancel the running turn — the explicit act a disconnect no longer performs.

        The cancellation lands in `run_turn` so the normal teardown runs. Awaited, so the caller's
        200 means "stopped". A teardown that raised is logged and suppressed: the turn is stopped
        either way.
        """
        # An explicit stop is immediate whatever is pending: the window was for a reload, and
        # this is somebody pressing Stop (or the window itself expiring, which released it first).
        self._drop_pending_stop()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            # Distinguish the turn's cancellation from this handler's own: only in the first case is
            # the task `cancelled()`, and a cancellation addressed to this frame must propagate.
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

    Entries are removed by the task's done callback, so a finished turn never answers `running`.
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

    def live(self) -> list[tuple[str, DetachableTurn]]:
        """Every session with a turn running here, and the turn — a snapshot, safe to iterate.

        Polled by `api/turn_relay.TurnRelay` to answer other replicas' requests for these turns.
        """
        return [(session_id, turn) for session_id, turn in self._turns.items() if turn.running]

    async def drain(self, timeout: float) -> int:
        """Wait up to `timeout` for every live pump to finish; report how many did not.

        Pump tasks are not HTTP requests, so uvicorn's drain does not see them; without this,
        shutdown would close the pools under running turns. Snapshots the registry, since completion
        deletes from it. Cancels nothing: each turn is bounded by its own deadline. Turns still
        running are reported.
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
