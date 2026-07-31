"""Asking instead of guessing, and asking what *would* happen (gaps AGT-5, IDEA-4).

AGT-5: `_INSTRUCTIONS` tells the agent to say plainly when the data is silent, but there was no
contract for it to *ask*. An ambiguous question therefore produced a best-guess sweep across every
matching reading — worse and more expensive than asking which one was meant.

IDEA-4: every expensive path is idempotent and cached, but there was no way to ask "what would you
do, what would it cost" without doing it — a natural primitive for a deployment whose production
default autonomy is `plan_only`.

The property that matters for the dry run is that it is **ambient, not a tool argument**: the model
must be able to neither set it (turning a real request into a no-op) nor clear it (turning a
requested dry run into a real HPC submission).
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from agent_framework import AgentSession

from chemclaw.agent.dialogue_tools import (
    ask_clarifying_question,
    dry_run_notice,
    is_dry_run,
    reset_dry_run,
    set_dry_run,
)
from chemclaw.api.runner import run_turn


class _AskingAgent:
    """An agent whose turn asks the chemist to disambiguate."""

    mcp_tools: list[Any] = []

    def run(  # noqa: D102 - a fake agent's run, documented by its class
        self,
        message: str,
        *,
        stream: bool,
        session: AgentSession,
        **_run_options: Any,
    ) -> Any:
        async def _gen() -> Any:
            await ask_clarifying_question("Which campaign?", ["proj-x", "proj-y"])
            yield SimpleNamespace(text="", contents=[], user_input_requests=[])

        return _gen()


def _events(agent: Any, **kwargs: Any) -> list[Any]:
    async def _collect() -> list[Any]:
        return [e async for e in run_turn(agent, AgentSession(session_id="s1"), "hi", **kwargs)]

    return asyncio.run(_collect())


def test_a_clarifying_question_reaches_the_surface() -> None:
    """The chemist sees the question and its options, rather than a guessed answer."""
    events = _events(_AskingAgent())
    question = next(e for e in events if e.type == "question")
    assert question.question == "Which campaign?"
    assert question.options == ["proj-x", "proj-y"]


def test_asking_off_the_request_path_is_a_no_op() -> None:
    """The CLI calls the same tools with no turn in flight; that must not blow up."""
    assert asyncio.run(ask_clarifying_question("anything")).startswith("Question put")


def test_dry_run_defaults_off_everywhere() -> None:
    """A missing flag must never mean "dry" — that would silently stop doing real work."""
    assert is_dry_run() is False


def test_dry_run_is_ambient_for_the_turn_only() -> None:
    """Set at the request boundary, cleared at teardown — like the ambient session/identity."""
    token = set_dry_run(True)
    try:
        assert is_dry_run() is True
    finally:
        reset_dry_run(token)
    assert is_dry_run() is False


def test_the_runner_binds_and_clears_the_flag() -> None:
    """A dry-run turn must not leak the flag into the next turn on the same worker."""
    seen: list[bool] = []

    class _Probe:
        mcp_tools: list[Any] = []

        def run(  # noqa: D102 - a fake agent's run, documented by its class
            self,
            message: str,
            *,
            stream: bool,
            session: AgentSession,
            **_run_options: Any,
        ) -> Any:
            async def _gen() -> Any:
                seen.append(is_dry_run())
                yield SimpleNamespace(text="ok", contents=[], user_input_requests=[])

            return _gen()

    _events(_Probe(), dry_run=True)
    assert seen == [True]
    assert is_dry_run() is False, "the flag leaked past the turn"


def test_the_notice_can_never_be_mistaken_for_a_real_result() -> None:
    """The one genuinely harmful failure mode: a dry-run answer read as a real one."""
    notice = dry_run_notice("submit a QM job", "CCO at B3LYP/def2-SVP")
    assert notice.startswith("DRY RUN")
    assert "Nothing was started" in notice


def test_a_dry_run_does_not_launch_a_durable_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: the expensive path is described, not taken."""
    from chemclaw.agent import durable_tools

    async def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a dry run reached the Temporal client")

    monkeypatch.setattr(durable_tools, "connect", _explode)
    monkeypatch.setattr(durable_tools, "require_actor", lambda: "u1")
    token = set_dry_run(True)
    try:
        result = asyncio.run(durable_tools.request_development_report("A report", []))
    finally:
        reset_dry_run(token)
    assert result.startswith("DRY RUN")


def test_a_dry_run_does_not_submit_a_qm_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same guard on the most expensive tool in the system, now that it is a declared job.

    Worth keeping as its own case rather than folding into the generic launcher tests: the QM job
    is the one whose side effect is a cluster reservation, and a dry run that reached the Temporal
    client would start one.
    """
    from chemclaw.connectors import jobs as connector_jobs
    from chemclaw.connectors.jobs import build_job_tool
    from chemclaw.connectors.registry import enabled

    (spec,) = next(m for m in enabled() if m.name == "qm").jobs

    async def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a dry run reached the Temporal client")

    monkeypatch.setattr(connector_jobs, "connect", _explode)
    tool = build_job_tool("qm", spec)
    params = tool.__annotations__["params"]
    token = set_dry_run(True)
    try:
        result = asyncio.run(
            tool(
                params(molecule_smiles="CCO", method="B3LYP", basis_set="def2-SVP"),
                "the reviewer questioned the reported barrier",
            )
        )
    finally:
        reset_dry_run(token)
    assert isinstance(result, str) and result.startswith("DRY RUN")
