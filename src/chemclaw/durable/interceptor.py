"""One worker interceptor, so every activity in this system says that it ran and how it ended.

Around every activity it binds the turn's actor, session and correlation id to the ambient
context (so worker log lines and audit rows are attributed), logs `activity.started` and
`activity.finished`, counts failed attempts and drain cancellations, tracks activities in
flight, and refuses a result too large for the broker. An interceptor, so a new activity is
instrumented the day it is written.

The ids are read from the activity's arguments: fields with the standard names on an argument
model (one level into a nested identity), or identically named `str` parameters of the activity
function's own signature. A model-authored payload can never supply an identity, and roles are
never taken from arguments at all.
"""

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import Iterator, Sequence
from typing import Any

from temporalio import activity
from temporalio.worker import ActivityInboundInterceptor, ExecuteActivityInput, Interceptor

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import (
    reset_current_correlation_id,
    reset_current_identity,
    set_current_correlation_id,
    set_current_identity,
)
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id

logger = logging.getLogger(__name__)

# The field names this codebase carries the three ids under, in order of preference.
_ACTOR_FIELDS = ("requested_by", "actor")
_SESSION_FIELDS = ("session_id",)
_CORRELATION_FIELDS = ("correlation_id",)
# Where a nested identity model hides (`identity: StepIdentity` on template step inputs).
_NESTED_FIELDS = ("identity",)
# The same names read as parameters of the activity function. Some activities carry identity as
# bare strings beside a model-authored payload (so it cannot change the payload's cache-key
# digest). The names come from the function's signature, never from data.
_NAMED_IDENTITY_PARAMETERS = frozenset(_ACTOR_FIELDS + _SESSION_FIELDS + _CORRELATION_FIELDS)

# Activities running now, and whether the worker is draining. No lock: one event loop per worker.
_IN_FLIGHT = 0
_DRAINING = False


def activities_in_flight() -> int:
    """How many activities this worker is currently executing.

    Read by `durable/serve.py` when a stop signal arrives; the SDK exposes no such count.
    """
    return _IN_FLIGHT


@contextlib.contextmanager
def draining() -> Iterator[None]:
    """Mark this worker as draining, so a cancelled activity is attributed to the drain.

    A flag rather than a before/after subtraction, since both readings would be taken after
    `Worker.shutdown()` returned; the cancellation itself is the event.
    """
    global _DRAINING
    _DRAINING = True
    try:
        yield
    finally:
        _DRAINING = False


class ActivityContext:
    """The three ambient ids one activity execution runs under, and its roles if it has any."""

    __slots__ = ("actor", "correlation_id", "roles", "session_id")

    def __init__(
        self,
        actor: str = "",
        roles: frozenset[str] = frozenset(),
        session_id: str = "",
        correlation_id: str = "",
    ) -> None:
        """Hold the ids as plain strings; empty means "this activity's input did not say"."""
        self.actor = actor
        self.roles = roles
        self.session_id = session_id
        self.correlation_id = correlation_id


def _models(args: Sequence[Any]) -> Iterator[Any]:
    """Every argument that could carry an id, and one level into a nested identity field.

    No deeper: an unbounded walk would read model-authored `payload` dictionaries, where an `actor`
    key would be something the LLM could fill in.
    """
    for arg in args:
        if arg is None or isinstance(arg, (str, bytes, int, float, bool)):
            continue
        yield arg
        for field in _NESTED_FIELDS:
            nested = getattr(arg, field, None)
            if nested is not None:
                yield nested


def _first(models: Sequence[Any], fields: Sequence[str]) -> str:
    """The first non-empty string any of `models` carries under any of `fields`."""
    for model in models:
        for field in fields:
            value = getattr(model, field, None)
            if isinstance(value, str) and value:
                return value
    return ""


def _named_strings(fn: Any, args: Sequence[Any]) -> dict[str, str]:
    """The identity-bearing *parameters* of `fn` that this call bound to a non-empty string.

    Positional only, as Temporal invokes an activity. An unreadable signature yields nothing rather
    than raising, since this runs around every activity.
    """
    try:
        parameters = list(inspect.signature(fn).parameters)
    except (TypeError, ValueError):  # pragma: no cover - no such activity exists in this tree
        return {}
    return {
        name: value
        # `strict=False`: an activity with defaulted identity parameters is invoked with
        # fewer arguments than it declares, which is the ordinary case here.
        for name, value in zip(parameters, args, strict=False)
        if name in _NAMED_IDENTITY_PARAMETERS and isinstance(value, str) and value
    }


def activity_context(args: Sequence[Any], fn: Any = None) -> ActivityContext:
    """The turn context an activity's own arguments carry, for logging and attribution.

    Public so it can be tested without a broker. Roles are never taken from the payload: actor,
    session and correlation are attribution, not authority, and are lifted; roles bind as an empty
    set, which every gate treats as fail-closed.
    """
    models = list(_models(args))
    # Roles are NOT taken from the payload: a relayed workflow argument is not a verified claim, and
    # anyone who can enqueue an activity could otherwise forge a privileged role. A durable job that
    # needs a user's entitlements was authorized at the front door before the workflow started;
    # carrying roles across the boundary safely would need a signed payload. Until then, fail
    # closed.
    named = _named_strings(fn, args) if fn is not None else {}

    def _named(fields: Sequence[str]) -> str:
        return next((named[field] for field in fields if field in named), "")

    return ActivityContext(
        actor=_first(models, _ACTOR_FIELDS) or _named(_ACTOR_FIELDS),
        roles=frozenset(),
        session_id=_first(models, _SESSION_FIELDS) or _named(_SESSION_FIELDS),
        correlation_id=_first(models, _CORRELATION_FIELDS) or _named(_CORRELATION_FIELDS),
    )


class ActivityResultTooLarge(ChemclawError):
    """An activity produced a result the broker will refuse to store, so it is refused here first.

    A `ChemclawError`, hence non-retryable: the result is deterministic, so a retry would be refused
    identically while the workflow sat `RUNNING`. The message names the size, the ceiling and the
    setting, since the fix is to bound the output or raise both ceilings.
    """


def _result_payload_bytes(result: Any) -> int:
    """The serialized size of an activity's result, as the broker will count it.

    Measured through the activity's own payload converter, so codecs and the configured data
    converter are accounted for; `ByteSize()` because the blob limit charges metadata too.
    """
    payloads = activity.payload_converter().to_payloads([result])
    return sum(payload.ByteSize() for payload in payloads)


class _ObservedActivity(ActivityInboundInterceptor):
    """Bind the turn's ids, record the attempt, and say how it ended — around every activity."""

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        """Run the activity inside the ambient context its own argument declares."""
        info = activity.info()
        context = activity_context(input.args, input.fn)
        # `Any` rather than `object`: it is splatted into `log_event`'s `**fields` beside typed
        # keyword-only parameters.
        fields: dict[str, Any] = {
            "activity": info.activity_type,
            "attempt": info.attempt,
            "task_queue": info.task_queue,
            "workflow_id": info.workflow_id or "",
            "run_id": info.workflow_run_id or "",
        }
        # Bound and counted inside the `try`, because the `finally` is what unbinds them; the tokens
        # are
        # declared before it so the `finally` can see them.
        identity_token: tuple[object, object] | None = None
        session_token: object = None
        correlation_token: object | None = None
        started = time.perf_counter()
        global _IN_FLIGHT
        try:
            # First inside the `try`: it cannot raise, and the `finally` decrements unconditionally.
            _IN_FLIGHT += 1
            identity_token = (
                set_current_identity(context.actor, context.roles) if context.actor else None
            )
            session_token = set_current_session_id(context.session_id or None)
            correlation_token = (
                set_current_correlation_id(context.correlation_id)
                if context.correlation_id
                else None
            )
            log_event(
                logger,
                "activity.started",
                "%s attempt %d on %s",
                info.activity_type,
                info.attempt,
                info.task_queue,
                **fields,
            )
            result = await self.next.execute_activity(input)
            # The result upload happens after this method returns, outside this `try`, so the size
            # is checked
            # before the return. Inside the `try` so a refusal is counted, logged and reaches the
            # workflow.
            size = _result_payload_bytes(result)
            if size > settings.activity_result_max_bytes:
                raise ActivityResultTooLarge(
                    f"{info.activity_type} produced a {size}-byte result, over the "
                    f"{settings.activity_result_max_bytes}-byte ceiling the broker would refuse "
                    "it at (CHEMCLAW_ACTIVITY_RESULT_MAX_BYTES); bound the activity's output or "
                    "raise both this ceiling and the broker's limit.blobSize.error"
                )
        except BaseException as exc:
            elapsed = time.perf_counter() - started
            # One row per attempt, so a retry storm is visible. A cancellation is not a failure (a
            # graceful
            # drain would otherwise page `ChemclawActivityRetryStorm`); it has its own counter
            # below.
            if not isinstance(exc, asyncio.CancelledError):
                record_metric(
                    lambda m: m.increment(
                        "chemclaw_activity_failures_total", labels={"activity": info.activity_type}
                    )
                )
            if _DRAINING and isinstance(exc, asyncio.CancelledError):
                # Temporal redelivers the work, so it is paid for twice; counted here because only
                # this frame sees
                # the cancellation.
                record_metric(
                    lambda m: m.increment("chemclaw_worker_activities_cancelled_on_drain_total")
                )
            # WARNING, not ERROR: a failed attempt is usually retried successfully; the job's own
            # failure
            # record is the one worth paging on.
            log_event(
                logger,
                "activity.finished",
                "%s attempt %d failed after %.3fs: %s",
                info.activity_type,
                info.attempt,
                elapsed,
                type(exc).__name__,
                level=logging.WARNING,
                exc_info=True,
                outcome="failed",
                error=type(exc).__name__,
                duration_ms=round(elapsed * 1000, 3),
                **fields,
            )
            raise
        else:
            elapsed = time.perf_counter() - started
            log_event(
                logger,
                "activity.finished",
                "%s attempt %d completed in %.3fs",
                info.activity_type,
                info.attempt,
                elapsed,
                outcome="completed",
                duration_ms=round(elapsed * 1000, 3),
                **fields,
            )
            return result
        finally:
            _IN_FLIGHT -= 1
            # Unbound in reverse order, each only if it was bound, so one run's identity never leaks
            # into the
            # next activity.
            if correlation_token is not None:
                reset_current_correlation_id(correlation_token)
            if session_token is not None:
                reset_current_session_id(session_token)
            if identity_token is not None:
                reset_current_identity(identity_token)


class ChemclawWorkerInterceptor(Interceptor):
    """Install `_ObservedActivity` around every activity this worker serves.

    Registered on every `Worker(...)` in the tree (`durable/background_worker.py`,
    `connectors/worker.py`).
    """

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        """Wrap the SDK's activity interceptor chain."""
        return _ObservedActivity(next)
