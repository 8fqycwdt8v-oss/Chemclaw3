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
from collections.abc import AsyncIterator
from typing import Any

import pytest
from agent_framework import AgentSession

from chemclaw.agent.dialogue_tools import ask_clarifying_question
from chemclaw.agent.tool_authz import DryRunRefusal, refuse_writes_on_dry_run
from chemclaw.agent.turn_flags import is_dry_run, reset_dry_run, set_dry_run
from chemclaw.api.runner import run_turn
from tests.fakes_turn import Piece, ScriptedTurn


class _AskingAgent(ScriptedTurn):
    """An agent whose turn asks the chemist to disambiguate."""

    async def stream(self, message: str) -> AsyncIterator[Piece]:  # noqa: D102 - see the base class
        await ask_clarifying_question("Which campaign?", ["proj-x", "proj-y"])
        yield ""


def _events(agent: ScriptedTurn, **kwargs: Any) -> list[Any]:
    """One turn's events on whichever engine is configured, with no connectors."""

    async def _collect() -> list[Any]:
        return [
            e
            async for e in run_turn(
                agent,
                AgentSession(session_id="s1"),
                "hi",
                connectors=[],
                graph_factory=agent.graph_factory,
                **kwargs,
            )
        ]

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

    class _Probe(ScriptedTurn):
        """A turn that records whether the dry-run flag was ambient while it ran."""

        async def stream(  # noqa: D102 - see `ScriptedTurn`
            self, message: str
        ) -> AsyncIterator[Piece]:
            seen.append(is_dry_run())
            yield "ok"

    _events(_Probe(), dry_run=True)
    assert seen == [True]
    assert is_dry_run() is False, "the flag leaked past the turn"


class _Function:
    """The one attribute the gate reads off an invocation context's function: its name."""

    def __init__(self, name: str) -> None:
        self.name = name


class _Context:
    """The slice of `FunctionInvocationContext` the dry-run gate touches."""

    def __init__(self, tool: str) -> None:
        self.function = _Function(tool)
        self.arguments: dict[str, Any] = {}
        self.result: Any = None


def _call(tool: str) -> bool:
    """Drive one tool call through the dry-run gate; return whether the tool body ran."""
    ran = False

    async def _body() -> None:
        nonlocal ran
        ran = True

    asyncio.run(refuse_writes_on_dry_run(_Context(tool), _body))  # type: ignore[arg-type]
    return ran


def test_a_dry_run_refuses_every_write_not_just_the_three_that_remembered() -> None:
    """The gate is the control; three tools checking for themselves was three that happened to.

    `propose_knowledge_note` is the case that made this necessary: it pushes a branch to the
    knowledge repository and had no dry-run check at all, so `dry_run: true` mutated the graph.
    """
    token = set_dry_run(True)
    try:
        for tool in ("propose_knowledge_note", "record_confirmed_answer", "remember_preference"):
            with pytest.raises(DryRunRefusal):
                _call(tool)
        # And the three that did check are still refused, now by the same gate.
        with pytest.raises(DryRunRefusal):
            _call("request_development_report")
    finally:
        reset_dry_run(token)


def test_a_dry_run_leaves_reads_alone() -> None:
    """A rehearsal that could not look anything up would be useless, and `plan_only` needs reads."""
    token = set_dry_run(True)
    try:
        assert _call("gather_evidence") is True
        assert _call("find_notes") is True
    finally:
        reset_dry_run(token)


def test_a_normal_turn_is_untouched() -> None:
    """A no-op off a dry run — including off the request path, where the flag is always False."""
    assert _call("propose_knowledge_note") is True


def test_the_refusal_can_never_be_mistaken_for_a_real_result() -> None:
    """The one genuinely harmful failure mode: a dry-run answer read as a real one.

    It reaches the model as a refusal rather than a return value, so
    `surface_authorization_denials` relays it verbatim — the path `PlanNotApprovedError` already
    proves works — instead of MAF's opaque "Function failed."
    """
    token = set_dry_run(True)
    try:
        with pytest.raises(DryRunRefusal) as refusal:
            _call("propose_knowledge_note")
    finally:
        reset_dry_run(token)
    message = str(refusal.value)
    assert message.startswith("DRY RUN")
    assert "Nothing was started" in message
