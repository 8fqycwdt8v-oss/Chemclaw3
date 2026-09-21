"""The per-turn ambients every driver of a turn has to open, in one place that cannot be half-used.

**Why this module exists: a cap is only as wide as the watch under it, and two of the three drivers
opened none.** `agent/loop_cap.py`'s `_LoopWatch.calls` is what makes a `task` fan-out share one
iteration allowance rather than getting one each — measured at a cap of 4 over 8 helpers, **25**
model calls following `1 + W*(cap - 1)`, and **193** at the shipped cap of 25 and width 8. Without a
watch the cap falls back to the per-branch channel snapshot, and `SubAgentMiddleware` hands every
helper the same pre-superstep state, so W branches each spend the whole allowance. The same gap
existed for the spend cap's ambient, which is what books the model calls a *tool body* makes: those
reach no `wrap_model_call`, and `agent/spend_cap.py` measured 5,200 tokens spent against a 150-token
budget with the cap never firing.

`api/runner.py` opened all four. `durable/template_activities.py` opened one, and
`cli/chat.py` opened none — so on those two paths a template step's fan-out and a CLI turn were
bounded by the fallback rather than by the turn. Four zero-argument, token-returning watches of
identical shape, opened by hand in each driver, is a thing three callers get wrong in three
different ways; one context manager is a thing a driver either enters or does not.

**What is deliberately *not* here.** The front door also stamps the session, the authenticated
identity, the correlation id, the dry-run flag and the chemist's own words — all of which need
arguments only a request has, and none of which a Temporal activity or a REPL has any business
inventing. `api/runner._turn_ambient` keeps those and delegates the caps here, so the two halves
compose without this module growing a signature that only one caller can fill.

**Synchronous on purpose**, because `api/runner._turn_ambient` is and must stay so: its resets are
reached by cancellation on the disconnect path (D-130), and an `await` between the last statement
and a reset re-raises the cancellation on the spot and leaks one turn's ambient identity into the
next turn on the worker. `tests/test_disconnect_teardown.py` asserts that shape, so this one is a
plain `@contextmanager` and not an async one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from chemclaw.agent.context_budget import begin_context_watch, end_context_watch
from chemclaw.agent.loop_cap import begin_loop_watch, end_loop_watch
from chemclaw.agent.repeat_guard import begin_call_watch, end_call_watch
from chemclaw.agent.spend_cap import begin_spend_watch, end_spend_watch
from chemclaw.agent.turn_usage import TurnUsage, reset_turn_usage, set_turn_usage

logger = logging.getLogger(__name__)


def reset_tolerantly(reset: Callable[[Any], None], token: Any, *, closing: str) -> None:
    """Undo one ambient, tolerating a token whose `Context` is not the one closing the turn.

    A contextvar `Token` remembers the `Context` it was created in, and one teardown path closes the
    turn from somewhere else: when a client stops reading, the turn's generator is abandoned at a
    `yield` and asyncio's async-generator finalizer runs `aclose()` in a *new task with a new
    context*. Every reset then raises `ValueError` — and the first one aborted the ones after it,
    including `reset_current_identity`, while surfacing as an unretrieved-task traceback naming a
    `ContextVar` and no session.

    Tolerating it loses nothing: the context those tokens belong to is being discarded either way,
    so the values are gone whether or not the reset lands. What is gained is that the *rest* of the
    teardown runs, and that the log line names what was being closed. Only `ValueError` — anything
    else from a reset is a real defect and must not be swallowed.

    Lives here rather than in `api/runner.py`, where it was written, because the cap ambients below
    need exactly the same tolerance for exactly the same reason, and a second copy of a function
    whose whole docstring is one subtle hazard is the "one fact declared twice" defect this package
    keeps finding. `tests/test_layering.py` forbids `agent -> api`, so the shared home is this side.

    Args:
        reset: The contextvar reset to attempt.
        token: The token `reset` takes.
        closing: What is being torn down, for the log line — a session id at the front door, the
            step or command elsewhere. Never a value worth redacting.
    """
    try:
        reset(token)
    except ValueError:
        logger.warning(
            "the turn for %s was torn down in a foreign context; its ambient %s could not be reset",
            closing,
            reset.__name__,
        )


@contextmanager
def turn_caps(usage: TurnUsage | None = None, *, closing: str = "a turn") -> Iterator[TurnUsage]:
    """Open every per-turn cap ambient, and close all of them however the turn ends.

    Yields the turn's `TurnUsage` ledger, so a driver that wants to book what the turn spent reads
    the same object the caps were enforced against rather than a second one. A caller that already
    holds a ledger passes it — `durable/template_activities.py`'s `_StepMeter` builds one per step
    — and a caller that does not gets a fresh one it may ignore.

    The four watches are opened in the order `api/runner.py` opened them and torn down in that same
    order, which is the order that was already shipped and reviewed; nothing here depends on it, and
    saying so is cheaper than leaving a reader to wonder.

    **A watch is not the cap.** `_harness_middleware` attaches both caps unconditionally, so a turn
    without this manager is still capped — by the per-branch channel and the message count, which is
    the pre-existing fallback rather than no bound at all. What this adds is that a *fan-out* shares
    one allowance and that an off-stream model call is counted.
    """
    ledger = usage if usage is not None else TurnUsage()
    calls_token = begin_call_watch()
    context_token = begin_context_watch()
    loop_token = begin_loop_watch()
    spend_token = begin_spend_watch()
    usage_token = set_turn_usage(ledger)
    try:
        yield ledger
    finally:
        reset_tolerantly(end_call_watch, calls_token, closing=closing)
        reset_tolerantly(end_context_watch, context_token, closing=closing)
        reset_tolerantly(end_loop_watch, loop_token, closing=closing)
        reset_tolerantly(end_spend_watch, spend_token, closing=closing)
        reset_tolerantly(reset_turn_usage, usage_token, closing=closing)
