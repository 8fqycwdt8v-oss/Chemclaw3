"""GxP tool-audit trail: record every agent tool call once, from one place.

Why this exists: in a pharma/GxP setting "who ran what, with which inputs, when, did it
succeed, and to what effect" must be answerable, and it is the first thing needed to
troubleshoot an agent turn. Rather than sprinkle logging into each of the ~13 tools
(duplication that would drift), one MAF **function middleware** wraps *every* registered
tool uniformly — the audit trail is a single reusable piece (DRY), like the PR-gate.

It is observe-only: it never alters the arguments or the result. Each call records the
correlation id (which conversation), the actor (who — a Phase-6 seam, `"unknown"` until
Entra identity lands), the tool name, its truncated arguments, the outcome and a short
effect summary (e.g. the PR ref a `propose_*` tool returned), and the wall-clock latency.
Records go to the stdlib log always, and additionally to a durable `AuditSink` when one is
supplied (the Postgres append-only trail) — the log is the floor, the sink is the GxP record.

Note: tool arguments and confirmed-answer payloads are user free text, so audit records may
contain PII. `agent_audit_max_arg_chars` bounds what is stored; treat the trail accordingly.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from agent_framework import FunctionInvocationContext, function_middleware
from pydantic import BaseModel

from chemclaw.config import settings

logger = logging.getLogger(__name__)


class AuditEvent(BaseModel):
    """One recorded tool invocation — the row an `AuditSink` persists."""

    correlation_id: str
    actor: str
    tool: str
    arguments: str
    outcome: str  # "ok" | "error"
    detail: str = ""  # result summary on success, exception text on failure
    latency_ms: float


@runtime_checkable
class AuditSink(Protocol):
    """Durable destination for audit events. Backends implement this (append-only)."""

    async def record(self, event: AuditEvent) -> None:
        """Persist one audit event. Must not raise into the tool call path."""
        ...


class NullAuditSink:
    """The default sink: the stdlib log is the only record (no durable store wired)."""

    async def record(self, event: AuditEvent) -> None:
        """Discard the event — logging in the middleware already recorded it."""
        return None


def _truncate(value: object) -> str:
    """Render a value as a single-line string bounded by the configured budget.

    A tool argument or result can be a large object (a full optimization problem, an
    evidence sweep); truncating keeps one audit record from ballooning while still
    identifying the call and its effect.
    """
    text = repr(value)
    limit = settings.agent_audit_max_arg_chars
    return text if len(text) <= limit else text[:limit] + "…"


def make_audit_middleware(
    *,
    correlation_id: str,
    actor: str,
    sink: AuditSink | None = None,
) -> Callable[[FunctionInvocationContext, Callable[[], Awaitable[None]]], Awaitable[None]]:
    """Build the tool-audit middleware bound to one conversation's identity.

    `correlation_id` ties every event to a single agent conversation; `actor` is who ran
    it (Phase-6 seam). `sink` is the durable trail — omitted (or `NullAuditSink`) means
    log-only. A sink failure is logged and swallowed: the audit store must never break a
    tool call.
    """
    audit_sink: AuditSink = sink if sink is not None else NullAuditSink()

    @function_middleware
    async def audit_tool_calls(
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        """Record one audit event per tool invocation (observe-only)."""
        name = context.function.name
        args = _truncate(context.arguments)
        start = time.perf_counter()
        try:
            await call_next()
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            logger.warning(
                "tool %s failed after %.0f ms [cid=%s actor=%s]: %s (args=%s)",
                name,
                elapsed_ms,
                correlation_id,
                actor,
                exc,
                args,
            )
            await _emit(
                audit_sink,
                AuditEvent(
                    correlation_id=correlation_id,
                    actor=actor,
                    tool=name,
                    arguments=args,
                    outcome="error",
                    detail=_truncate(exc),
                    latency_ms=elapsed_ms,
                ),
            )
            raise
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        detail = _truncate(context.result) if context.result is not None else ""
        logger.info(
            "tool %s ok in %.0f ms [cid=%s actor=%s] (args=%s)",
            name,
            elapsed_ms,
            correlation_id,
            actor,
            args,
        )
        await _emit(
            audit_sink,
            AuditEvent(
                correlation_id=correlation_id,
                actor=actor,
                tool=name,
                arguments=args,
                outcome="ok",
                detail=detail,
                latency_ms=elapsed_ms,
            ),
        )

    return audit_tool_calls


async def _emit(sink: AuditSink, event: AuditEvent) -> None:
    """Persist an event, never letting a sink failure escape into the tool path."""
    try:
        await sink.record(event)
    except Exception as exc:  # a broken audit store must not fail a tool call
        logger.warning("audit sink failed to record %s: %s", event.tool, exc)


# The default, log-only middleware for the credential-free path (and the direct unit tests):
# no conversation id, no identity, no durable sink. `build_agent` builds a per-conversation
# middleware with a real correlation id (and an optional durable sink) instead.
audit_tool_calls = make_audit_middleware(correlation_id="-", actor="unknown")
