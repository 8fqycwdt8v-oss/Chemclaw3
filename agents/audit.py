"""GxP tool-audit trail: log every agent tool call once, from one place.

Why this exists: in a pharma/GxP setting "who ran what, with which inputs, and did it
succeed" must be answerable, and it is the first thing needed to troubleshoot an agent turn.
Rather than sprinkle logging into each of the ~13 tools (duplication that would drift), one
MAF **function middleware** wraps *every* registered tool uniformly — the audit trail is a
single reusable piece (DRY), exactly like the PR-gate and the retriever interface.

It is observe-only: it never alters the arguments or the result, it just records the tool
name, its (truncated) arguments, the outcome, and the wall-clock latency on top of the stdlib
logging floor (`chemclaw.logging`). A successful call logs at INFO; a failing tool logs at
WARNING and the exception is re-raised unchanged so the agent's own error handling is intact.
"""

import logging
import time
from collections.abc import Awaitable, Callable

from agent_framework import FunctionInvocationContext, function_middleware

from chemclaw.config import settings

logger = logging.getLogger(__name__)


def _summarize_args(arguments: object) -> str:
    """Render tool arguments as a single-line, length-bounded string for the audit log.

    A tool argument can be a large object (a full optimization problem, a list of
    observations); truncating to the configured budget keeps one audit line from flooding
    the log while still identifying the call.
    """
    text = repr(arguments)
    limit = settings.agent_audit_max_arg_chars
    return text if len(text) <= limit else text[:limit] + "…"


@function_middleware
async def audit_tool_calls(
    context: FunctionInvocationContext,
    call_next: Callable[[], Awaitable[None]],
) -> None:
    """Log one audit line per tool invocation: name, args, outcome, latency (observe-only)."""
    name = context.function.name
    args = _summarize_args(context.arguments)
    start = time.perf_counter()
    try:
        await call_next()
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        logger.warning("tool %s failed after %.0f ms: %s (args=%s)", name, elapsed_ms, exc, args)
        raise
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    logger.info("tool %s ok in %.0f ms (args=%s)", name, elapsed_ms, args)
