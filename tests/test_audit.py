"""The tool-audit middleware records every call, once, without altering behavior.

Proves the GxP audit trail: a successful tool call is logged at INFO with its name and
arguments, a failing one is logged at WARNING and the exception propagates unchanged, and
oversized arguments are truncated to the configured budget. The middleware only touches
`context.function.name` and `context.arguments`, so a light stand-in context is enough — no
live agent run or model call is needed.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import cast

import pytest
from agent_framework import FunctionInvocationContext

from agents.audit import audit_tool_calls
from chemclaw.config import settings


def _ctx(name: str, arguments: object) -> FunctionInvocationContext:
    """A minimal stand-in exposing only the fields the middleware reads."""
    return cast(
        FunctionInvocationContext,
        SimpleNamespace(function=SimpleNamespace(name=name), arguments=arguments),
    )


def _drive(ctx: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
    """Run the middleware over a stand-in context to completion."""

    async def _run() -> None:
        await audit_tool_calls(ctx, call_next)

    asyncio.run(_run())


async def _ok() -> None:
    """A tool body that succeeds."""
    return None


async def _boom() -> None:
    """A tool body that raises."""
    raise ValueError("boom")


def test_audit_logs_a_successful_call(caplog: pytest.LogCaptureFixture) -> None:
    """A successful invocation logs one INFO line naming the tool and its arguments."""
    with caplog.at_level(logging.INFO):
        _drive(_ctx("predict_solubility", {"smiles": "CCO"}), _ok)
    assert "tool predict_solubility ok" in caplog.text
    assert "CCO" in caplog.text  # the argument is captured for the audit trail


def test_audit_logs_and_reraises_a_failure(caplog: pytest.LogCaptureFixture) -> None:
    """A failing tool logs at WARNING and the original exception propagates unchanged."""
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ValueError, match="boom"):
            _drive(_ctx("compute_xtb_energy", {}), _boom)
    assert "tool compute_xtb_energy failed" in caplog.text
    assert "boom" in caplog.text


def test_audit_truncates_oversized_arguments(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large argument payload is truncated to the configured budget, not logged whole."""
    monkeypatch.setattr(settings, "agent_audit_max_arg_chars", 10)
    with caplog.at_level(logging.INFO):
        _drive(_ctx("gather_evidence", {"q": "x" * 500}), _ok)
    assert "…" in caplog.text  # truncation marker present
    assert "x" * 100 not in caplog.text  # the full payload never reaches the log
