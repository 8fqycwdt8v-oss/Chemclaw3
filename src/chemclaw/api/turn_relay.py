"""A running turn, followed and stopped from a replica that does not hold it.

A turn's pump lives in the process that started it (`api/detach.DetachableTurn`), so other replicas
reach it through rows (`agent/turn_remotes.TurnRemotes`):

- **The holding process** runs `TurnRelay.run` while it holds a turn (an idle replica issues
  nothing). It answers requests with the same in-process calls its routes make: a follow attaches an
  ordinary `DetachableTurn.watch` view — so caps, lag cut-off and unload-stop resume treat it
  exactly as a local one — and relays its frames into rows; a stop calls `stop()` or `defer_stop()`.
- **The asking replica** authorizes first (same session gate, same sender-or-owner rule), writes the
  request, waits for the answer, and streams the relayed frames (`follow`) or reports the outcome
  (`stop`).

A request is a lease on a conversation already in flight, not durable work.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncGenerator, Coroutine
from typing import Any, NamedTuple

import psycopg

from chemclaw.agent.turn_remotes import Holding, Kind, Request, State, TurnRemotes
from chemclaw.api.detach import DetachableTurn, RunningTurns, Watch, _draft_call_id, lagged_frame
from chemclaw.api.state import TurnLease, claim_holder
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS

logger = logging.getLogger(__name__)

#: How many frames one relayed view may hold unwritten before it is cut off as lagged — the
#: same bound as a local reader's buffer, protecting the turn from a stalled consumer.
_RELAY_BACKLOG = 1024


class Answer(NamedTuple):
    """The holder's answer to a request: its state, and the turn's correlation id for a follow."""

    state: State | None
    correlation_id: str


class TurnRelay:
    """Both halves of reaching a turn across replicas, over one `TurnRemotes` store."""

    def __init__(
        self,
        store: TurnRemotes,
        running: RunningTurns,
        active_turns: dict[str, TurnLease],
    ) -> None:
        """Serve the turns in `running` (claimed under the leases in `active_turns`) to others."""
        self._store = store
        self._running = running
        self._active = active_turns
        # Request id → the turn it follows and the task relaying a view of it, while it is open.
        self._relays: dict[str, tuple[tuple[str, str], asyncio.Task[None]]] = {}
        # Stops in flight and withdrawals not yet landed, held so neither is garbage-collected.
        self._background: set[asyncio.Task[None]] = set()

    def _spawn(self, work: Coroutine[Any, Any, None], name: str) -> None:
        """Run `work` on a task this relay holds a reference to until it ends."""
        task = asyncio.get_running_loop().create_task(work, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # -- the holding process -------------------------------------------------------------------

    async def run(self) -> None:
        """Answer other replicas' requests for this process's turns, every poll, until cancelled.

        A failed poll is logged and retried next interval, so a database blip delays one remote Stop
        rather than ending the loop.
        """
        while True:
            try:
                await self.poll()
            except (ConnectionError, psycopg.Error):
                METRICS.increment("chemclaw_turn_relay_poll_failures_total")
                logger.warning(
                    "could not read the requests other replicas addressed to this process's turns; "
                    "retrying in %ss",
                    settings.service_turn_relay_poll_seconds,
                    exc_info=True,
                )
            await asyncio.sleep(settings.service_turn_relay_poll_seconds)

    def _held(self) -> dict[tuple[str, str], tuple[DetachableTurn, TurnLease]]:
        """Every turn this process holds, keyed by the `(session_id, holder)` its claim carries."""
        held: dict[tuple[str, str], tuple[DetachableTurn, TurnLease]] = {}
        for session_id, turn in self._running.live():
            lease = self._active.get(session_id)
            if lease is not None:
                held[(session_id, claim_holder(lease.token))] = (turn, lease)
        return held

    async def poll(self) -> None:
        """Read the requests addressed to the turns held here, and answer the new ones."""
        held = self._held()
        if not held and not self._relays:
            return
        requests = await self._store.pending(list(held))
        live = {request.id for request in requests}
        # A view whose request is gone was withdrawn or lapsed: stop relaying — but only for a turn
        # still held here, since a just-ended turn's relay is about to write its answer and end
        # marker.
        for request_id, (turn_key, task) in list(self._relays.items()):
            if turn_key in held and request_id not in live:
                task.cancel()
        for request in requests:
            entry = held.get((request.session_id, request.holder))
            if request.state == "asked" and entry is not None:
                await self._serve(request, *entry)

    async def _serve(self, request: Request, turn: DetachableTurn, lease: TurnLease) -> None:
        """Answer one new request with the call the local route would have made."""
        if request.kind == "watch":
            await self._attach(request, turn)
            return
        grace = settings.service_turn_unload_grace_seconds
        if (
            request.kind == "unload_stop"
            and grace > 0
            and turn.defer_stop(
                grace,
                resumers=frozenset(oid for oid in (request.actor, lease.actor) if oid),
                max_deferrals=settings.service_turn_unload_grace_max_deferrals,
            )
        ):
            METRICS.increment("chemclaw_turns_stop_deferred_total")
            logger.info(
                "session %s's turn will be stopped in %ss unless its page reattaches (an unload "
                "stop sent to another replica)",
                request.session_id,
                grace,
            )
            await self._store.answer(request.id, "deferred")
            return
        # Answered before the cancel, so the next poll does not serve the same request twice.
        await self._store.answer(request.id, "stopping")
        self._spawn(self._stop(request, turn), name=f"turn-remote-stop:{request.session_id}")

    async def _stop(self, request: Request, turn: DetachableTurn) -> None:
        """Stop the turn, then tell the asker it is stopped — its teardown has run by then."""
        await turn.stop()
        METRICS.increment("chemclaw_turns_stopped_total")
        logger.info(
            "session %s's turn was stopped by request from another replica", request.session_id
        )
        try:
            await self._store.answer(request.id, "stopped")
        except (ConnectionError, psycopg.Error):
            logger.warning(
                "session %s's turn is stopped but the asking replica could not be told; it reports "
                "the stop as delivered when its wait ends",
                request.session_id,
                exc_info=True,
            )

    async def _attach(self, request: Request, turn: DetachableTurn) -> None:
        """Open a view of the turn for a watcher on another replica, or say why not."""
        if turn.watchers >= settings.service_turn_max_watchers:
            await self._store.answer(request.id, "refused")
            return
        watch = turn.watch(request.actor)
        if watch is None:
            await self._store.answer(request.id, "gone")
            return
        try:
            await self._store.answer(request.id, "watching", turn.correlation_id)
        except BaseException:
            watch.close()
            raise
        # The sender reattaching remotely cancels a pending unload stop as a local reattach does.
        turn.resume(request.actor)
        task = asyncio.get_running_loop().create_task(
            self._relay(request.id, watch), name=f"turn-relay:{request.session_id}"
        )
        self._relays[request.id] = ((request.session_id, request.holder), task)
        task.add_done_callback(lambda _t: self._relays.pop(request.id, None))

    async def _relay(self, request_id: str, watch: Watch) -> None:
        """Copy one view's frames into rows for its asker, a poll's worth at a time, to its end.

        The view is drained into a list as fast as the turn produces, so the turn never waits on
        the database; the list is written once per poll. A newer draft of an artefact replaces the
        unwritten one of the same call, as a local reader's buffer does (`detach._DraftSlot`). A
        backlog the database is not taking ends the view as lagged rather than growing without
        bound. The end of the view is relayed as a `NULL` frame.
        """
        backlog: list[dict[str, str]] = []
        # The draft call id of each backlog entry, aligned with it, so coalescing never re-parses.
        calls: list[str | None] = []
        ended = asyncio.Event()

        async def _consume() -> None:
            """Drain the view into the backlog until it ends or the backlog overflows."""
            try:
                async for frame in watch.events:
                    call_id = _draft_call_id(frame)
                    if call_id is not None and call_id in calls:
                        backlog[calls.index(call_id)] = frame
                        continue
                    if len(backlog) >= _RELAY_BACKLOG:
                        METRICS.increment("chemclaw_turn_readers_lagged_total")
                        backlog.append(lagged_frame())
                        calls.append(None)
                        return
                    backlog.append(frame)
                    calls.append(call_id)
            finally:
                ended.set()

        consumer = asyncio.get_running_loop().create_task(_consume())
        try:
            while True:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(ended.wait(), settings.service_turn_relay_poll_seconds)
                finished = ended.is_set()
                taken = len(backlog)
                batch: list[dict[str, str] | None] = list(backlog[:taken])
                if finished:
                    batch.append(None)
                try:
                    await self._store.relay(request_id, batch)
                except psycopg.errors.ForeignKeyViolation:
                    return  # the asker withdrew between two polls; nobody is reading
                except (ConnectionError, psycopg.Error):
                    logger.warning(
                        "could not relay a turn's frames to another replica; retrying next poll",
                        exc_info=True,
                    )
                    await asyncio.sleep(settings.service_turn_relay_poll_seconds)
                    continue
                # Only what was written: the consumer may have appended behind it meanwhile.
                del backlog[:taken]
                del calls[:taken]
                if finished:
                    return
        finally:
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consumer
            watch.close()

    # -- the asking replica --------------------------------------------------------------------

    async def holding(self, session_id: str) -> Holding | None:
        """Who holds `session_id`'s turn right now, wherever it runs; `None` when nobody does."""
        return await self._store.holding(session_id)

    async def _ask(
        self, session_id: str, holding: Holding, kind: Kind, actor: str, settled: frozenset[str]
    ) -> tuple[str, Answer]:
        """Address a request to the holder and wait for an answer in `settled`.

        The wait also ends when the holder stops holding the turn, or when the lease runs out.

        Returns the request id (still live, for the caller to withdraw) and the last answer read:
        `state=None` when the turn ended under the request, `"asked"` when the holder never
        answered within `service_turn_relay_lease_seconds`.
        """
        lease = settings.service_turn_relay_lease_seconds
        request_id = await self._store.ask(session_id, holding.holder, kind, actor, lease)
        try:
            deadline = time.monotonic() + lease
            while True:
                answer = await self._store.refresh(request_id, lease)
                if answer is None:
                    return request_id, Answer(None, "")
                if answer[0] in settled:
                    return request_id, Answer(*answer)
                current = await self._store.holding(session_id)
                if current is None or current.holder != holding.holder:
                    # The turn ended; read the answer once more, since a stop's teardown releases
                    # the claim before the holder writes `stopped`. Only an unanswered request means
                    # "not running".
                    last = await self._store.refresh(request_id, lease)
                    if last is None or last[0] == "asked":
                        return request_id, Answer(None, "")
                    return request_id, Answer(*last)
                if time.monotonic() >= deadline:
                    return request_id, Answer(*answer)
                await asyncio.sleep(settings.service_turn_relay_poll_seconds)
        except BaseException:
            self.forget(request_id)
            raise

    def forget(self, request_id: str) -> None:
        """Withdraw a request without waiting — safe from a `finally` that is being cancelled."""
        self._spawn(self._withdraw(request_id), name="turn-remote-withdraw")

    async def _withdraw(self, request_id: str) -> None:
        """Delete the request; a failure leaves it to lapse after one lease, which is logged."""
        try:
            await self._store.withdraw(request_id)
        except (ConnectionError, psycopg.Error):
            logger.warning(
                "could not withdraw a request to another replica's turn; it lapses on its own",
                exc_info=True,
            )

    async def stop(self, session_id: str, holding: Holding, actor: str, *, unload: bool) -> Answer:
        """Ask the holder to stop the turn, and wait until it has (or has deferred the stop).

        `"stopping"` at the end of the wait means the cancel reached the turn and its teardown was
        still running when the lease ran out; the caller reports it as stopped, because the turn
        will not produce anything more.
        """
        request_id, answer = await self._ask(
            session_id,
            holding,
            "unload_stop" if unload else "stop",
            actor,
            frozenset({"stopped", "deferred"}),
        )
        self.forget(request_id)
        return answer

    async def follow(self, session_id: str, holding: Holding, actor: str) -> tuple[str, Answer]:
        """Ask the holder for a view of the turn; the request id and the holder's answer.

        On `"watching"` the caller streams `view(request_id, ...)` and withdraws the request when
        its response ends; on any other answer the request is already withdrawn.
        """
        request_id, answer = await self._ask(
            session_id, holding, "watch", actor, frozenset({"watching", "refused", "gone"})
        )
        if answer.state != "watching":
            self.forget(request_id)
        return request_id, answer

    async def view(
        self, request_id: str, session_id: str, holder: str
    ) -> AsyncGenerator[dict[str, str], None]:
        """The relayed frames of one followed turn, polled from rows until the holder ends them.

        If the holder dies (its claim gone for one quiet lease) or the request vanishes, the view
        ends without a final event, as a local view does when its process dies; the reattach that
        follows answers `410 turn_interrupted`.
        """
        lease = settings.service_turn_relay_lease_seconds
        quiet_since = time.monotonic()
        try:
            while True:
                answer, frames = await self._store.poll(request_id, lease)
                for frame in frames:
                    if frame is None:
                        return
                    yield frame
                if answer is None:
                    return
                now = time.monotonic()
                if frames:
                    quiet_since = now
                elif now - quiet_since >= lease:
                    current = await self._store.holding(session_id)
                    if current is None or current.holder != holder:
                        return
                    quiet_since = now
                await asyncio.sleep(settings.service_turn_relay_poll_seconds)
        finally:
            self.forget(request_id)
