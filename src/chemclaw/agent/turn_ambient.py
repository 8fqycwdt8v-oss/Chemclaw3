"""The per-turn cap ambients every driver of a turn opens, behind one context manager.

The loop cap's and spend cap's watches are what make a `task` fan-out share one allowance:
without them each helper branch reads its own pre-superstep snapshot and spends the whole budget.
The usage ledger additionally counts model calls made inside tool bodies, but only where a meter
fills it (the front door and template steps; the CLI's stays empty). Every driver — `api/runner`,
`durable/template_activities`, `cli/chat` — enters `turn_caps`, so none can open half of them.

Request-scoped ambients (session, identity, correlation id, dry-run) stay in
`api/runner._turn_ambient`. This module is synchronous on purpose: the runner's resets run on the
disconnect path, where an `await` re-raises the cancellation and leaks one turn's ambient identity
into the next turn on the worker.
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

    When a client stops reading, the turn's generator is finalised by `aclose()` in a new task with
    a
    new context, so every reset raises `ValueError`; without tolerance the first one aborts the
    rest,
    including `reset_current_identity`. The context is being discarded anyway, so nothing is lost by
    skipping it. Only `ValueError` is tolerated. Lives here so `api/runner` and the cap ambients
    share
    it (`agent` may not import `api`).

    Args:
        reset: The contextvar reset to attempt.
        token: The token `reset` takes.
        closing: What is being torn down, for the log line (a session id, step or command).
    """
    try:
        reset(token)
    except ValueError:
        logger.warning(
            "the turn for %s was torn down in a foreign context; its ambient %s was not reset",
            closing,
            reset.__name__,
        )


@contextmanager
def turn_caps(usage: TurnUsage, *, closing: str = "a turn") -> Iterator[TurnUsage]:
    """Open every per-turn cap ambient, and close all of them however the turn ends.

    Yields the turn's `TurnUsage` ledger, so a driver books spend from the same object the caps
    read.
    `usage` is required so a nested call cannot shadow an outer turn's ledger. Without this manager
    a
    turn is still capped, by the per-branch channel; this makes a fan-out share one allowance and,
    where
    a meter fills the ledger, counts off-stream model calls.
    """
    calls_token = begin_call_watch()
    context_token = begin_context_watch()
    loop_token = begin_loop_watch()
    spend_token = begin_spend_watch()
    usage_token = set_turn_usage(usage)
    try:
        yield usage
    finally:
        reset_tolerantly(end_call_watch, calls_token, closing=closing)
        reset_tolerantly(end_context_watch, context_token, closing=closing)
        reset_tolerantly(end_loop_watch, loop_token, closing=closing)
        reset_tolerantly(end_spend_watch, spend_token, closing=closing)
        reset_tolerantly(reset_turn_usage, usage_token, closing=closing)
