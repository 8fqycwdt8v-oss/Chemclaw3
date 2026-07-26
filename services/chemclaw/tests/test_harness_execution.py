"""The harness's plan/execute/todo loop actually runs — real MAF machinery, not wiring only (D-040).

`tests/test_agent.py` proves the harness is *constructed* correctly (providers attached, toolset
kept, start mode set) with a dummy `object()` client and no LLM call. This file goes one step
further: `ScriptedChatClient` is a real `BaseChatClient` (mixed with MAF's
`FunctionInvocationLayer`, exactly as `OpenAIChatClient` and the other concrete clients are), so
`build_agent`'s actual harness wiring — `TodoProvider`, `AgentModeProvider`,
`AgentLoopMiddleware`/`todos_remaining` — drives a genuine multi-iteration autonomous loop: the
scripted model adds todos, the loop re-invokes it while any remain open, it completes them one by
one, and the loop stops itself once none are left. Nothing about the loop, the todo store, or the
completion predicate is mocked — only the model's replies are scripted, standing in for a live LLM.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from typing import Any

import pytest
from agent_framework import (
    DEFAULT_TODO_SOURCE_ID,
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    ResponseStream,
    TodoSessionStore,
)
from agent_framework._tools import FunctionInvocationLayer

from agents.chemclaw_agent import build_agent
from chemclaw.config import settings

# One scripted turn: given the messages sent to the model, return its next reply.
_ScriptedTurn = Callable[[list[Message]], ChatResponse]


class ScriptedChatClient(FunctionInvocationLayer, BaseChatClient):
    """A real chat client whose replies are a fixed script, standing in for a live LLM.

    `FunctionInvocationLayer` is mixed in (as every concrete MAF client does) so the framework's
    own tool-calling loop recognizes and executes the scripted `function_call` content against the
    real registered tools (here, the harness's `todos_add`/`todos_complete`) — this is not a fake
    of the tool-execution mechanism, only of the model's replies.
    """

    def __init__(self, script: Sequence[_ScriptedTurn]) -> None:
        """Start with the given reply script, consumed one entry per model call."""
        super().__init__()
        self._script = list(script)
        self.calls: list[list[Message]] = []

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Awaitable[ChatResponse] | ResponseStream[ChatResponseUpdate, ChatResponse]:
        """Pop and return the next scripted reply, recording the messages it was called with."""
        sent = list(messages)
        self.calls.append(sent)
        response = self._script.pop(0)(sent)
        if stream:

            async def _updates() -> Any:
                yield ChatResponseUpdate(contents=response.messages[0].contents, role="assistant")

            return ResponseStream(_updates(), finalizer=lambda _updates: response)

        async def _await_response() -> ChatResponse:
            return response

        return _await_response()


def _text(text: str) -> _ScriptedTurn:
    """A scripted turn that replies with plain text (no tool call)."""

    def _reply(_messages: list[Message]) -> ChatResponse:
        return ChatResponse(
            messages=[Message(role="assistant", contents=[Content.from_text(text)])],
            response_id="r",
        )

    return _reply


def _call(call_id: str, name: str, arguments: dict[str, object]) -> _ScriptedTurn:
    """A scripted turn that replies with one function call."""

    def _reply(_messages: list[Message]) -> ChatResponse:
        return ChatResponse(
            messages=[
                Message(
                    role="assistant",
                    contents=[Content.from_function_call(call_id, name, arguments=arguments)],
                )
            ],
            response_id="r",
        )

    return _reply


def _two_step_script() -> list[_ScriptedTurn]:
    """Plan two todos, then complete them one per iteration — the scenario every test drives."""
    return [
        _call("c1", "todos_add", {"todos": [{"title": "step one"}, {"title": "step two"}]}),
        _text("planned two steps"),
        _call("c2", "todos_complete", {"items": [{"id": 1, "reason": "did step one"}]}),
        _text("finished step one"),
        _call("c3", "todos_complete", {"items": [{"id": 2, "reason": "did step two"}]}),
        _text("all steps done"),
    ]


def _run_turn(agent: object, message: str, session: object) -> str:
    """Run one streamed turn to completion and return its final text.

    Opens/closes the agent's MCP capability servers for the turn, exactly as `service.runner.
    run_turn` and `agents.cli` do — the lifecycle `build_agent`'s docstring leaves to its caller.
    """

    async def _run() -> str:
        async with AsyncExitStack() as stack:
            for tool in getattr(agent, "mcp_tools", None) or []:
                await stack.enter_async_context(tool)
            stream = agent.run(message, stream=True, session=session)  # type: ignore[attr-defined]
            async for _update in stream:
                pass
            final = await stream.get_final_response()
        return str(final.text)

    return asyncio.run(_run())


def test_execute_autonomy_loops_through_todos_to_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`execute` autonomy: the scripted model plans two todos and the loop drives both to done.

    Proof, not assertion-on-wiring: the loop re-invokes the model twice more (2 open todos → 1 open
    → 0 open) purely because `todos_remaining` reads real todo-store state after each iteration —
    nothing here tells the loop how many times to run.
    """
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "execute")
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 10)
    client = ScriptedChatClient(_two_step_script())
    agent = build_agent(chat_client=client)
    session = agent.create_session()

    final_text = _run_turn(agent, "do the two-step task", session)

    assert final_text == "all steps done"
    assert len(client.calls) == 6  # 3 loop iterations, each one tool call + one text reply
    items = asyncio.run(TodoSessionStore().load_items(session, source_id=DEFAULT_TODO_SOURCE_ID))
    assert len(items) == 2
    assert all(item.is_complete for item in items)


def test_plan_only_autonomy_does_not_auto_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`plan_only` (the default autonomy) never auto-loops past the plan.

    Two todos are left open — the pre-execution approval gate actually holds the loop back, not
    just a cosmetically different `default_mode` value.
    """
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "plan_only")
    client = ScriptedChatClient(
        [
            _call("c1", "todos_add", {"todos": [{"title": "step one"}, {"title": "step two"}]}),
            _text("here is my plan; let me know when to proceed"),
        ]
    )
    agent = build_agent(chat_client=client)
    session = agent.create_session()

    final_text = _run_turn(agent, "do the two-step task", session)

    assert final_text == "here is my plan; let me know when to proceed"
    assert len(client.calls) == 2  # exactly one iteration: the plan, then stop for approval
    items = asyncio.run(TodoSessionStore().load_items(session, source_id=DEFAULT_TODO_SOURCE_ID))
    assert len(items) == 2
    assert not any(item.is_complete for item in items)  # left open, awaiting human approval


def test_loop_is_capped_by_the_configured_max_iterations(monkeypatch: pytest.MonkeyPatch) -> None:
    """A todo that the model never completes stops at `harness_max_loop_iterations`, not forever."""
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "execute")
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 2)
    # The model adds one todo and then only ever replies with text — it never completes it, so
    # only the iteration cap can end the loop.
    script = [_call("c1", "todos_add", {"todos": [{"title": "never finished"}]})] + [
        _text("still working on it") for _ in range(10)
    ]
    client = ScriptedChatClient(script)
    agent = build_agent(chat_client=client)
    session = agent.create_session()

    _run_turn(agent, "do a task", session)

    # 3 client calls, not the 11 scripted: iteration 1 is a tool call + its follow-up text (2
    # calls), each further iteration is text-only (1 call) — 2 + (max_iterations - 1) * 1 = 3.
    assert len(client.calls) == 3
    items = asyncio.run(TodoSessionStore().load_items(session, source_id=DEFAULT_TODO_SOURCE_ID))
    assert not items[0].is_complete  # the cap stopped the loop, not the model finishing the todo
