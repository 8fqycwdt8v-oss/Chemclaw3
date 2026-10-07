"""The push-back mailbox's readers: the job stream, the standing-query digest, and check-ins.

`session_events` is one durable mailbox, claimed per kind. `GET /sessions/{id}/events` streams
session pushes over SSE, gated by `resolve_session`. `GET /digests` and `GET /check-ins` are single
destructive reads of a mailbox derived from the caller's `oid`, so there is nothing to authorize.
"""

import logging
import random
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sse_starlette.sse import EventSourceResponse, SendTimeoutError
from starlette.types import Receive, Scope, Send

from chemclaw.agent.session_events import SessionEvent, claim_unconsumed
from chemclaw.api import app as front_door
from chemclaw.api.deps import CurrentUser, resolve_session
from chemclaw.api.events import (
    TURN_EVENT_REF,
    AwaitingAnswerEvent,
    ErrorEvent,
    ExhibitEvent,
    JobCompletedEvent,
    JobFailedEvent,
    sse_frame,
)
from chemclaw.api.state import _take_event_stream_slot, state
from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_correlation_id
from chemclaw.core.metrics import METRICS
from chemclaw.durable.awaiting import AWAITING_KIND
from chemclaw.durable.check_in import CHECK_IN_KIND
from chemclaw.durable.digest import DIGEST_KIND, digest_channel
from chemclaw.exhibits.models import PUSH_KIND as EXHIBIT_PUSH_KIND

logger = logging.getLogger(__name__)

#: How far one tailer's poll interval is spread either side of `session_event_poll_seconds`.
#:
#: Constant intervals keep in-phase tailers in phase, so a pod's streams would hit the pool
#: as one burst every poll. Drawn once per stream, which de-phases a fleet within a couple of
#: polls; the cost is a job notice up to 25% later than the configured interval. A constant,
#: not a setting: it is a property of how pollers de-phase, not a deployment choice.
_POLL_SPREAD = 0.25


def _spread_poll_interval() -> float:
    """This stream's own poll interval: the configured one, off-phase from every other stream."""
    interval = settings.session_event_poll_seconds
    return interval * random.uniform(1.0 - _POLL_SPREAD, 1.0 + _POLL_SPREAD)


def _newest_per_state(batch: list[SessionEvent]) -> list[SessionEvent]:
    """One claim's `awaiting-answer` rows reduced to the newest frame of each request-and-state.

    Unconsumed rows are never pruned, so one claim can carry many frames for a question (an open,
    its reminders, an expiry); a surface needs each request's current state. The reduction needs the
    batch, so it runs as `stream_new_events`' `collapse`. The surviving row is the last occurrence
    (consistent `payload` and `event_id`), emitted at the first occurrence's position (so different
    requests keep their order). Rows of other kinds pass through untouched.
    """
    newest: dict[tuple[str, str], SessionEvent] = {}
    for event in batch:
        if event.kind == AWAITING_KIND:
            newest[_awaiting_key(event)] = event
    seen: set[tuple[str, str]] = set()
    kept: list[SessionEvent] = []
    for event in batch:
        if event.kind != AWAITING_KIND:
            kept.append(event)
            continue
        key = _awaiting_key(event)
        if key in seen:
            continue
        seen.add(key)
        kept.append(newest[key])
    return kept


def _awaiting_key(event: SessionEvent) -> tuple[str, str]:
    """What makes two `awaiting-answer` rows the same fact: one request, one state.

    A missing `state` reads as `waiting`, matching `_awaiting_event`, so the expiry push's sparser
    payload does not become a key of its own.
    """
    return (
        str(event.payload.get("request_id", "")),
        str(event.payload.get("state", "waiting")),
    )


class _SlotBoundEventStream(EventSourceResponse):
    """An SSE response that holds its admission slot for exactly as long as it is being served.

    Released in `__call__`'s `finally`, which runs however the stream ends, including a cancel
    before the generator first advances (whose own `finally` would never run). Not a lease:
    push-back streams are unbounded in time. `send_timeout` stops a half-open connection parking the
    generator.
    """

    def __init__(
        self,
        content: AsyncIterator[dict[str, str]],
        *,
        release: Callable[[], None],
        ping: int,
        send_timeout: float,
        session_id: str,
    ) -> None:
        """Wrap `content`, bounding each send and releasing the slot when the response ends."""
        super().__init__(content, ping=ping, send_timeout=send_timeout)
        self._release = release
        self._session_id = session_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the stream, returning the admission slot however it ends.

        `SendTimeoutError` is caught here, as in `_TurnStream`, so it is logged with the session id
        instead of escaping as an ASGI traceback, and counted on its own series
        (`chemclaw_event_stream_send_timeouts_total`) so push-back drops are not mixed with turn
        drops.
        """
        try:
            await super().__call__(scope, receive, send)
        except SendTimeoutError:
            METRICS.increment("chemclaw_event_stream_send_timeouts_total")
            logger.warning(
                "the push-back client of session %s stopped reading for %ss; the stream was closed",
                self._session_id,
                settings.service_sse_send_timeout_seconds,
            )
        finally:
            self._release()


async def session_events(
    request: Request,
    session_id: str,
    principal: CurrentUser,
) -> EventSourceResponse:
    """Stream async job push-back for the session: a finished job wakes the chat.

    Bounded per user (`service_max_event_streams_per_user`) and per process
    (`service_max_event_streams_total`), since every stream polls the database for its whole life;
    429 past either cap. The claim is destructive (at-most-once) and scoped in SQL to the kinds read
    here — both job outcomes, `awaiting-answer`, and a person's artefact write — so other consumers'
    kinds are never destroyed.
    """
    # Read here, not where the error is built: the generator runs later in a copied context, and the
    # event's id must be this request's correlation id.
    correlation_id = get_current_correlation_id() or ""
    # Shared with the turn-watch route: a followed turn is held open just as long, so it counts
    # against the same caps.
    release_slot = _take_event_stream_slot(state(request).event_streams, principal.oid)
    if release_slot is None:
        METRICS.increment("chemclaw_event_streams_rejected_total")
        # `Retry-After` is required: the UI reads a 429 without it as an exhausted budget and locks
        # the composer, while this cap clears as soon as a stream closes.
        raise HTTPException(
            status_code=429,
            detail="too many concurrent event streams; close one and retry",
            headers={"Retry-After": "1"},
        )

    async def _events() -> AsyncIterator[dict[str, str]]:
        # The newest `awaiting_answer` state already sent to this client, per request; per
        # connection, so a reconnect re-reports.
        awaiting_reported: dict[str, str] = {}
        # No `finally` here: `_SlotBoundEventStream` owns the slot. Called through the module so the
        # suite's patch seam (`chemclaw.agent.session_events.stream_new_events`) reaches it.
        try:
            async for pushed in front_door.stream_new_events(
                session_id,
                kinds=("job_completed", "job_failed", AWAITING_KIND, EXHIBIT_PUSH_KIND),
                # Collapse one claim's redundant `awaiting-answer` rows; see `_newest_per_state`.
                collapse=_newest_per_state,
                # This stream's own interval, so idle tabs do not poll as one wavefront (see
                # `_POLL_SPREAD`).
                poll_seconds=_spread_poll_interval(),
            ):
                # A question the agent is waiting on is news on this channel too. The collapse above
                # handles one batch; this per-connection suppression stops a later poll re-reporting
                # an unchanged state. A state change (`waiting` → `expired`) is always sent.
                if pushed.kind == AWAITING_KIND:
                    frame = _awaiting_event(pushed.payload)
                    request_id = str(pushed.payload.get("request_id", ""))
                    state_now = str(pushed.payload.get("state", "waiting"))
                    if request_id and awaiting_reported.get(request_id) == state_now:
                        continue
                    if request_id:
                        awaiting_reported[request_id] = state_now
                    yield frame
                    continue
                if pushed.kind == EXHIBIT_PUSH_KIND:
                    # A person's artefact write (from `api/routes/exhibits.py`), so the session's
                    # other tabs learn of it. Best effort: the claim is at-most-once across tabs,
                    # and the pane refetches on focus.
                    exhibit = _exhibit_event(pushed.payload)
                    if exhibit is not None:
                        yield sse_frame(exhibit)
                    continue
                job_id = str(pushed.payload.get("job_id", ""))
                failed = pushed.kind == "job_failed"
                reason = str(pushed.payload.get("reason", ""))
                event: JobCompletedEvent | JobFailedEvent = (
                    JobFailedEvent(job_id=job_id, reason=reason)
                    if failed
                    else JobCompletedEvent(job_id=job_id, summary=pushed.payload)
                )
                yield sse_frame(event)
        except Exception as exc:
            # Once the response has started, the app's 503 handler for a failed Postgres checkout
            # cannot run, so report it here as a `storage_unavailable` `ErrorEvent` and count it on
            # the same metric the write side uses. `Exception` only, so a client disconnect
            # (`GeneratorExit`/`CancelledError`) is not reported as an outage.
            METRICS.increment("chemclaw_db_unavailable_total")
            logger.warning("push-back stream for session %s ended: %s", session_id, exc)
            lost = ErrorEvent(
                message=(
                    "The connection to the job stream was lost; reconnect to keep receiving "
                    f"results (session {session_id})."
                ),
                code="storage_unavailable",
                retryable=True,
                correlation_id=correlation_id,
            )
            yield sse_frame(lost)

    handed_off = False
    try:
        response = _SlotBoundEventStream(
            _events(),
            release=release_slot,
            ping=settings.service_sse_ping_seconds,
            send_timeout=settings.service_sse_send_timeout_seconds,
            session_id=session_id,
        )
        handed_off = True
        return response
    finally:
        # Any exception before hand-off must return the slot; after hand-off only the response
        # releases it.
        if not handed_off:
            release_slot()


class Digest(BaseModel):
    """One standing query's new matches, as the digest job left them in the caller's mailbox.

    **Two of the job's four fields used to stop here, and both were the ones a reader acts on.**
    `collect_digests` has computed `disputed` since `D-2026-08-27` — which notes among the matches
    the corpus now disagrees with — and writes it into the mailbox payload, and this model had no
    such field and `_digest` never read the key. The outbound delivery channels rendered it, so a
    deployment that had configured one saw "2 of 9 disagree with something already in the graph"
    and a deployment that had not — the shipped default, `CHEMCLAW_DELIVERY_CHANNELS` empty — lost
    it entirely on the only path a UI reads. `DigestItem`'s own docstring calls that asymmetry the
    reason the field exists: "a chemist who happens to ask is told, and a chemist watching the
    subject is not." It was still true, one layer further down.

    `headlines` is the other: without it this route answers with note **ids** and a client can do
    nothing but print them.
    """

    query: str = ""
    note_ids: list[str] = Field(default_factory=list)
    disputed: list[str] = Field(default_factory=list)
    headlines: dict[str, str] = Field(default_factory=dict)


def _exhibit_event(payload: dict[str, Any]) -> ExhibitEvent | None:
    """Read one claimed artefact push into its event, or `None` for a payload that is not one.

    Lenient, since the row is already claimed: a raise would destroy the rest of the batch. An
    invalid payload is logged and dropped.
    """
    try:
        return ExhibitEvent.model_validate(payload)
    except ValidationError:
        logger.warning("dropping an artefact push that is not an exhibit event: %r", payload)
        return None


def _whole(raw: object) -> int:
    """A count out of a mailbox payload, with no input this can raise on.

    An `isinstance` test rather than `int(...)`, which raises on strings and dicts: callers run
    after the claim, so a raise would destroy the row and the rest of the batch. `bool` is not a
    count; negatives are floored at zero.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return max(raw, 0)


def _awaiting_event(payload: dict[str, Any]) -> dict[str, str]:
    """Read one claimed `awaiting-answer` row into the SSE frame the contract declares.

    Lenient because the row is already claimed and will not be re-delivered; the request itself
    stays open in `GET /pending`. Every count goes through `_whole`. Both pushes carry `request_id`,
    `subject`, `state` and `reminders`; only the open adds `kind`, `asked_of` and `due_at`.
    """
    event = AwaitingAnswerEvent(
        request_id=str(payload.get("request_id", "")),
        state=str(payload.get("state", "waiting")),
        subject=str(payload.get("subject", "")),
        kind=str(payload.get("kind", "")),
        asked_of=str(payload.get("asked_of", "")),
        due_at=str(payload.get("due_at", "")),
        reminders=_whole(payload.get("reminders")),
    )
    return sse_frame(event)


def _digest(payload: dict[str, Any]) -> Digest:
    """Read one claimed mailbox row, tolerating a payload an older job wrote.

    Lenient because the row is already consumed: a missing key costs one blank field, a raised
    `ValidationError` the whole digest. The `isinstance` guards keep both old and new payload shapes
    readable.
    """
    note_ids = payload.get("note_ids")
    disputed = payload.get("disputed")
    headlines = payload.get("headlines")
    return Digest(
        query=str(payload.get("query", "")),
        note_ids=[str(note_id) for note_id in note_ids] if isinstance(note_ids, list) else [],
        disputed=[str(note_id) for note_id in disputed] if isinstance(disputed, list) else [],
        headlines=(
            {str(key): str(value) for key, value in headlines.items()}
            if isinstance(headlines, dict)
            else {}
        ),
    )


async def read_digests(principal: CurrentUser) -> list[Digest]:
    """Claim and return the standing-query digests waiting for the caller.

    The channel derives from the principal (`digest_channel`), so there is nothing to authorize. The
    read is the consume, scoped to `DIGEST_KIND`; a lost response loses only the notification.
    Unbounded because the claim has already run; the bound is one row per subscription per
    `digest_schedule_minutes`.
    """
    claimed = await claim_unconsumed(digest_channel(principal.oid), kinds=(DIGEST_KIND,))
    return [_digest(event.payload) for event in claimed]


class CheckInOut(BaseModel):
    """One question the caller asked that is still waiting on somebody.

    The workflow's own `BlockedRequest` restated at the wire rather than imported, for the reason
    every other model in this module is: `durable/check_in.py` is a worker-side shape free to gain
    fields a client has no business seeing, and an API model that *is* a durable payload makes the
    two impossible to move apart. The fields here are the ones a person acts on.

    **Three of them were missing and each cost the surface one thing it already does elsewhere**:
    without `kind` a check-in could not be badged by the class of answer it wants, while the
    pending inbox on the same page badges every row by exactly that field off `GET /pending`;
    without `session_id` a check-in row ended nowhere, where both other inboxes on that page end in
    "open the conversation", and matching `request_id` against `GET /pending` instead would be a
    join across two listings scoped to opposite people; and without `truncated` a chemist with more
    than `durable/check_in._PAGE_ROWS` open questions was shown a list that looks complete.

    Restating the worker's shape is what made adding them a decision rather than a leak, and it is
    also why two of the three needed a change on the worker side first: `session_id` was a column
    `_BLOCKED` did not select, and `truncated` was a `CheckIn` field `_tell` never wrote into the
    payload. Nothing here could have dropped what never arrived.
    """

    request_id: str = ""
    #: What class of answer the question wants — the same bounded vocabulary `GET /pending` sends.
    kind: str = ""
    subject: str = ""
    rationale: str = ""
    asked_of: str = ""
    open_days: int = 0
    days_left: int = 0
    #: The conversation the question was asked in, or `""`. Always one of the caller's own.
    session_id: str = ""
    #: Whether the notice this question arrived in was short of the asker's whole blocked set.
    #:
    #: A property of the claimed row, stamped on every entry it carried, since the answer is
    #: flattened across rows.
    truncated: bool = False


def _check_in(payload: dict[str, Any]) -> list[CheckInOut]:
    """Read one claimed check-in row, tolerating a payload an older sweep wrote.

    Lenient for `_digest`'s reason, with no way to re-find a lost notice afterwards; day counts go
    through `_whole` so a malformed payload cannot raise after the claim.
    """
    requests = payload.get("requests")
    if not isinstance(requests, list):
        return []
    # `is True`: the key is additive, so neither a missing key nor a stray string may claim the list
    # was short.
    truncated = payload.get("truncated") is True
    out: list[CheckInOut] = []
    for item in requests:
        if not isinstance(item, dict):
            continue
        out.append(
            CheckInOut(
                request_id=str(item.get("request_id", "")),
                kind=str(item.get("kind", "")),
                subject=str(item.get("subject", "")),
                rationale=str(item.get("rationale", "")),
                asked_of=str(item.get("asked_of", "")),
                open_days=_whole(item.get("open_days")),
                days_left=_whole(item.get("days_left")),
                session_id=str(item.get("session_id", "")),
                truncated=truncated,
            )
        )
    return out


async def read_check_ins(principal: CurrentUser) -> list[CheckInOut]:
    """Claim and return the caller's own blocked work, as the check-in sweep left it.

    A route of its own: folding these into `/digests` would break that response's shape, and the two
    share a mailbox and nothing else. Otherwise it follows `read_digests`: the channel comes from
    the principal, the read is the consume (scoped to `CHECK_IN_KIND`), and the answer is unbounded.
    `check_in_enabled` turns the sweep off.
    """
    claimed = await claim_unconsumed(digest_channel(principal.oid), kinds=(CHECK_IN_KIND,))
    return [item for event in claimed for item in _check_in(event.payload)]


def register(app: FastAPI) -> None:
    """Attach this module's route to `app` — called once, by `create_app` only.

    Registered on the app rather than via the lazy `include_router`, which would hide routes from
    tests that walk the route table and disable `app.dependency_overrides`.
    """
    app.get(
        "/sessions/{session_id}/events",
        dependencies=[Depends(resolve_session)],
        # FastAPI cannot infer a `text/event-stream` body, so the OpenAPI document states it.
        responses={
            200: {
                "description": "One SSE frame per pushed-back session event.",
                "content": {"text/event-stream": {"schema": {"$ref": TURN_EVENT_REF}}},
            }
        },
    )(session_events)
    # No `resolve_session` dependency: this route accepts no session id. See `read_digests`.
    app.get("/digests")(read_digests)
    # Same absence of a session dependency, for the same reason, one mailbox kind over.
    app.get("/check-ins")(read_check_ins)
