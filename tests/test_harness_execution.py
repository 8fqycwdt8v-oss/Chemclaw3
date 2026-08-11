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
from agent_framework._middleware import ChatMiddlewareLayer
from agent_framework._tools import FunctionInvocationLayer

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.chemclaw_agent import build_agent
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.loop_cap import begin_loop_watch, end_loop_watch, loop_hit_cap
from chemclaw.agent.message_pairing import calls_without_adjacent_results
from chemclaw.cli import chat as cli
from chemclaw.connectors.registry import open_reachable
from chemclaw.core.config import settings
from tests.fakes_langgraph import ScriptedChatModel

# One scripted turn: given the messages sent to the model, return its next reply.
_ScriptedTurn = Callable[[list[Message]], ChatResponse]


class ScriptedChatClient(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
    """A real chat client whose replies are a fixed script, standing in for a live LLM.

    The base list mirrors a concrete MAF client's own layering
    (`FunctionInvocationLayer` → `ChatMiddlewareLayer` → … → `BaseChatClient`), so the framework's
    tool-calling loop executes the scripted `function_call` content against the real registered
    tools (here `todos_add`/`todos_complete`) — this fakes the model's replies, nothing else.

    `ChatMiddlewareLayer` matters more than it looks: `BaseChatClient` is deliberately the base
    *without* middleware wrapping, and that layer is what consumes `client_kwargs["middleware"]`.
    Omitting it ran every harness test through a pipeline with **zero** chat middleware — so the
    two the harness itself installs (`MessageInjectionMiddleware` and
    `PerServiceCallHistoryPersistingMiddleware`) never executed here, and the tests passed green
    while the same code path failed 100% of the time against a real client.
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

    Connects/closes the agent's connectors for the turn through the same `open_reachable` helper
    `chemclaw.api.runner.run_turn` and `chemclaw.agent.cli` use — the lifecycle `build_agent`'s
    docstring leaves
    to
    its caller. Sharing the helper is what keeps this test honest: it exercises the real degrade
    path,
    so a connector that is not running (none is, here) costs its tools and not the turn.
    """

    async def _run() -> str:
        async with AsyncExitStack() as stack:
            await open_reachable(stack, getattr(agent, "mcp_tools", None) or [])
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


def test_a_capped_loop_says_so_and_a_completed_one_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap is observable from outside the loop — the fact `runaway_rate` had to guess at.

    `AgentLoopMiddleware` stops at `max_iterations` and returns normally, emitting nothing, so a
    capped turn was externally identical to a finished one. `chemclaw.api.runner` turns this
    question into an `ErrorEvent` coded `loop_cap_reached`; before that could mean anything, the
    question itself had to have an answer, and the answer comes from a real MAF loop here rather
    than from a stub. Both directions are asserted in one test on purpose: a signal that is always
    on is as useless as one that never fires, and the two scripts differ only in whether the model
    ever completes the todo it added.
    """
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "execute")
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 2)

    never_finished = [_call("c1", "todos_add", {"todos": [{"title": "never finished"}]})] + [
        _text("still working on it") for _ in range(10)
    ]
    capped_agent = build_agent(chat_client=ScriptedChatClient(never_finished))
    # Watched from out here, not inside the run: `asyncio.run` copies the context into its task, so
    # a flag *set* in there would never be seen out here — which is why the watch holds a mutable
    # record rather than rebinding the contextvar (`chemclaw.agent.loop_cap`, same reason as
    # `core.turn_signals`).
    token = begin_loop_watch()
    try:
        _run_turn(capped_agent, "do a task", capped_agent.create_session())
        assert loop_hit_cap() is True
    finally:
        end_loop_watch(token)

    # Raised *before* the agent is built: `loop_max_iterations` is read at construction, and a
    # two-step script under a cap of 2 is a capped loop rather than a completed one.
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 10)
    finishing_agent = build_agent(chat_client=ScriptedChatClient(_two_step_script()))
    token = begin_loop_watch()
    try:
        _run_turn(finishing_agent, "do the two-step task", finishing_agent.create_session())
        assert loop_hit_cap() is False
    finally:
        end_loop_watch(token)


def test_no_tool_call_reaches_the_model_without_its_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every `function_call` the model is sent carries its `function_result` (the harness 400).

    The live failure this pins: on the streaming path the harness sent Anthropic a transcript in
    which a `tool_use` block was followed by a `user` block instead of its `tool_result`, and the
    API rejected the whole request — `tool_use ids were found without tool_result blocks`. It was
    100% reproducible on any tool call, in both autonomy modes, so harness mode was unusable.

    Cause: `create_harness_agent` turns on per-service-call history persistence, whose middleware
    tells the function-invocation loop "I am injecting history, stop resending the transcript" by
    stamping a sentinel `conversation_id` on the finalized response. The harness *also* installs
    `MessageInjectionMiddleware`, which on the streaming path rebuilds the response via
    `ChatResponse.from_updates()` — and the sentinel, living on the inner response rather than on
    any streamed update, does not survive the rebuild. The loop then re-sent the full transcript
    while history was separately re-injected, and the duplicate put a `user` block between a call
    and its result.

    Asserted over the messages actually handed to the client, so it fails on the real defect
    rather than on a restatement of the fix.
    """
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "execute")
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 10)
    client = ScriptedChatClient(_two_step_script())
    agent = build_agent(chat_client=client)
    session = agent.create_session()

    _run_turn(agent, "do the two-step task", session)

    for index, sent in enumerate(client.calls):
        assert calls_without_adjacent_results(sent) == set(), (
            f"model call {index} was sent a tool call with no result immediately after: "
            f"{[(m.role, [c.type for c in m.contents]) for m in sent]}"
        )


def test_history_is_not_duplicated_across_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """The transcript is sent once per model call, not once per injection path.

    The same root cause seen from the other side: with the sentinel lost, the loop's own
    accumulated messages and the separately-injected history both landed in one request, so the
    turn's messages appeared twice and grew with every iteration. Counting the user's own message
    catches that without asserting on the framework's exact message shape.
    """
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "execute")
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 10)
    client = ScriptedChatClient(_two_step_script())
    agent = build_agent(chat_client=client)
    session = agent.create_session()

    _run_turn(agent, "do the two-step task", session)

    for index, sent in enumerate(client.calls):
        occurrences = sum(1 for m in sent if "do the two-step task" in (m.text or ""))
        assert occurrences <= 1, f"model call {index} repeated the user's message {occurrences}x"


def test_the_cli_takes_a_turn_under_the_shipped_harness_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cli.converse` completes a turn with `harness_enabled` on — the chart's configuration.

    The gap this closed is a composition, not a component (D-152, LIVE-8): the harness was covered,
    the CLI was covered, and the pair — which is precisely what the Helm chart deploys — was not.
    It surfaced on the first live run as `RuntimeError: ToolApprovalMiddleware requires an
    AgentSession`, before the model was reached.

    **The mechanism is gone and the composition is still worth a test.** That failure was MAF's
    harness refusing a session-less run; a graph turn takes a `thread_id` string, so there is
    nothing to be absent. What remains true is the reason the gap existed at all — the CLI and the
    harness flag are exercised separately everywhere else — so this keeps driving both together,
    against the configuration the chart actually sets.

    Offline on purpose: a scripted model makes the *on* state of a flag testable without a
    credential, which is the rule this whole class of defect keeps re-teaching.
    """
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "plan_only")
    model = ScriptedChatModel(["aspirin's pKa is about 3.5"])
    agent = build_langgraph_agent(model=model, audit_sink=NullAuditSink())

    answer = asyncio.run(cli.converse(agent, "what is aspirin's pKa?"))

    assert "3.5" in answer
