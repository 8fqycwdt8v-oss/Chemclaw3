"""One worker interceptor, so every activity in this system says that it ran and how it ended.

**The measurement this exists for.** Against a live broker on 2026-08-27, one
`ConnectorJobWorkflow` was run twice — once succeeding, once failing on a `ValueError`. The
successful job emitted **zero** log records; the failed one emitted zero first-party records and
moved no metric. The only output either produced was two `temporalio` SDK warnings. At the time
`grep -rn "activity.logger" src/` returned nothing, 39 of 43 activities logged nothing at all, and
the four that did used a plain module logger, so no line carried the workflow id, the attempt or
the task queue.

Two other absences met in the same place, which is why this is one object rather than three:

- **`set_current_correlation_id` had exactly one caller in the repository** — the front door
  (`api/runner.py`). Every line a worker wrote therefore rendered `correlation_id="-" actor="-"
  session_id="-"`, while `deploy/README.md` told an operator to join on those fields. The ids were
  never missing: they ride in the activity's own argument (`ConnectorJobInput.correlation_id`,
  `JobRecord.session_id`, `StepIdentity.actor`). Nothing bound them to the ambient context that
  `core/logging.py`'s `ContextFilter` reads.
- **A failed activity attempt was counted nowhere.** Temporal's own history knows about a retry
  storm; no series did, so "every attempt at this activity has failed for an hour" and "nobody has
  called it" were the same picture on every dashboard.

**Why an interceptor and not 43 edits.** An obligation that must hold for every activity belongs to
the one place they all run through — the same rule `ConnectorJobWorkflow` follows for the durable
record, the PR-gate and the push-back, and for the same reason: "each activity remembers" is the
discipline that fails silently. It also means a *new* activity is instrumented the day it is
written, with nothing to forget.

The binding is read from the activity's **arguments**, not from a header or a memo. An activity
cannot read its workflow's memo, and the argument is where this system already puts the three ids
— by an explicit design decision in each case (`ConnectorJobInput` documents why the actor travels
in the payload rather than on the transport). So the walk below looks for the field *names* this
codebase already standardised on, one level into a nested identity model, and binds nothing it does
not find.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterator, Sequence
from typing import Any

from temporalio import activity
from temporalio.worker import ActivityInboundInterceptor, ExecuteActivityInput, Interceptor

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

# The field names this codebase already carries the three ids under, in the order a walk should
# prefer them. Literals rather than a protocol because the models they name are unrelated —
# `ConnectorJobInput`, `JobRecord`, `JobPublishInput` and `StepIdentity` share no base beyond
# `BaseModel`, and giving them one would be an abstraction with four callers and no behaviour.
_ACTOR_FIELDS = ("requested_by", "actor")
_SESSION_FIELDS = ("session_id",)
_CORRELATION_FIELDS = ("correlation_id",)
# Where a nested identity model hides. `template_activities`' three step inputs carry theirs as
# `identity: StepIdentity` rather than flat, and that is the shape the template path — the one
# path whose failures were completely silent (J4) — actually uses.
_NESTED_FIELDS = ("identity",)

# How many activities this worker is running right now, and whether it is draining. Plain module
# state and no lock: a worker is one event loop in one process, so these are only ever touched from
# tasks on that loop.
_IN_FLIGHT = 0
_DRAINING = False


def activities_in_flight() -> int:
    """How many activities this worker is currently executing.

    Read by `durable/serve.py` at the moment a stop signal arrives, because "the drain was carrying
    nothing" and "the drain was carrying eleven ELN pages" are the two states its log line could not
    tell apart. The interceptor is the only place that knows: the SDK's worker exposes no count, and
    an activity's own body has no reason to keep one.
    """
    return _IN_FLIGHT


@contextlib.contextmanager
def draining() -> Iterator[None]:
    """Mark this worker as draining, so a cancelled activity is attributed to the drain.

    A flag rather than a before/after subtraction in `serve_worker`, because the two readings it
    would subtract are both taken *after* `Worker.shutdown()` has returned — by which time every
    cancelled activity has already unwound and the difference is indistinguishable from the
    activities that simply finished in time. The cancellation itself is the event, and this is the
    only frame that sees one.
    """
    global _DRAINING
    _DRAINING = True
    try:
        yield
    finally:
        _DRAINING = False


class ActivityContext:
    """The three ambient ids one activity execution should run under, and its roles if it has any.

    A tiny value object rather than a tuple because four fields positionally is exactly how the
    session id and the actor got stamped by different subsets of the template step activities —
    the drift `template_activities.stamp_identity` was written to end.
    """

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

    One level and no deeper, deliberately: the ids are declared at the top of an activity's input
    model or on the identity model it embeds, and an unbounded walk over arbitrary payloads would
    read model-authored `payload` dictionaries — where an `actor` key would be a field the LLM
    could fill in, which is precisely why `ConnectorJobInput` puts the real one beside the payload
    rather than in it.
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


def activity_context(args: Sequence[Any]) -> ActivityContext:
    """The turn context an activity's own arguments carry, for logging and attribution.

    Public because it is the whole testable part of this module: everything else needs a running
    activity, and "the ids the front door stamped reach the worker's log lines" is a property that
    should be checkable without a broker.

    **Roles are taken only where an input actually declares them.** Binding an actor with an empty
    role set is safe by construction — every gate that reads them fails closed on an empty set
    (`authz.check_expensive_action`, `documents/retriever._entitled`) — so the widening this could
    have been is not one. What it buys is that `agent/audit.py` and `kg/proposal.py`, which read
    the ambient actor and booked `""` for every row a worker ever wrote, now name the person the
    run was launched for.
    """
    models = list(_models(args))
    roles: frozenset[str] = frozenset()
    for model in models:
        declared = getattr(model, "roles", None)
        if isinstance(declared, (list, tuple, frozenset, set)) and declared:
            roles = frozenset(str(role) for role in declared)
            break
    return ActivityContext(
        actor=_first(models, _ACTOR_FIELDS),
        roles=roles,
        session_id=_first(models, _SESSION_FIELDS),
        correlation_id=_first(models, _CORRELATION_FIELDS),
    )


class _ObservedActivity(ActivityInboundInterceptor):
    """Bind the turn's ids, record the attempt, and say how it ended — around every activity."""

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        """Run the activity inside the ambient context its own argument declares."""
        info = activity.info()
        context = activity_context(input.args)
        # Typed `Any` rather than `object` because it is splatted into `log_event`'s `**fields`,
        # which sits beside two typed keyword-only parameters (`level`, `exc_info`) — a
        # `dict[str, object]` splat is a type error against those, and narrowing the values is the
        # honest fix rather than adding an ignore.
        fields: dict[str, Any] = {
            "activity": info.activity_type,
            "attempt": info.attempt,
            "task_queue": info.task_queue,
            "workflow_id": info.workflow_id or "",
            "run_id": info.workflow_run_id or "",
        }
        identity_token: tuple[object, object] | None = (
            set_current_identity(context.actor, context.roles) if context.actor else None
        )
        session_token = set_current_session_id(context.session_id or None)
        correlation_token = (
            set_current_correlation_id(context.correlation_id) if context.correlation_id else None
        )
        started = time.perf_counter()
        global _IN_FLIGHT
        _IN_FLIGHT += 1
        log_event(
            logger,
            "activity.started",
            "%s attempt %d on %s",
            info.activity_type,
            info.attempt,
            info.task_queue,
            **fields,
        )
        try:
            result = await self.next.execute_activity(input)
        except BaseException as exc:
            elapsed = time.perf_counter() - started
            # One row **per attempt**, which is the whole point: a counter booked once per
            # activity would report a retry storm as a single failure, and the storm is the part
            # an operator can act on.
            record_metric(
                lambda m: m.increment(
                    "chemclaw_activity_failures_total", labels={"activity": info.activity_type}
                )
            )
            if _DRAINING and isinstance(exc, asyncio.CancelledError):
                # The work is not lost — Temporal redelivers it — but it is paid for twice, which
                # is the cost `durable/serve.py`'s docstring names and which nothing measured. It
                # is counted here rather than in the drain, because the cancellation is the event
                # and this is the only frame that sees one.
                record_metric(
                    lambda m: m.increment("chemclaw_worker_activities_cancelled_on_drain_total")
                )
            # WARNING and not ERROR: an attempt that fails is usually retried and the retry usually
            # works, so this is a caution about something that may matter. The *job* failing is a
            # separate record with its own outcome (`connector_job.py`), and that one is the one
            # worth paging on.
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
            # Unbound in the reverse order they were bound, and unconditionally: a contextvar left
            # set leaks one run's identity into whatever this worker picks up next, which is the
            # failure `template_activities.stamp_identity` already carries a `finally` for.
            if correlation_token is not None:
                reset_current_correlation_id(correlation_token)
            reset_current_session_id(session_token)
            if identity_token is not None:
                reset_current_identity(identity_token)


class ChemclawWorkerInterceptor(Interceptor):
    """Install `_ObservedActivity` around every activity this worker serves.

    Registered on every `Worker(...)` in the tree (`durable/background_worker.py`,
    `connectors/worker.py`), because a worker that serves work without recording it is exactly the
    state this module's docstring measured — and a second worker wiring only some of the
    cross-cutting concerns is the failure `durable/serve.py` was written to prevent for probes and
    shutdown.
    """

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        """Wrap the SDK's activity interceptor chain."""
        return _ObservedActivity(next)
