"""The testing CLI resolves identity, parses args, and runs a turn (chemclaw/cli/chat.py).

Credential-free: the run path uses a stub agent or a real graph over a fake model, so this proves
CLI plumbing (admin-only auth gate, actor resolution, answer extraction), not model behaviour.
"""

import asyncio
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage

from chemclaw.agent import plan_approval_store as store_module
from chemclaw.agent import plan_state
from chemclaw.agent.checkpointer import process_checkpointer
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.plan_approval_store import Decision, InMemoryPlanApprovalStore
from chemclaw.agent.plan_gate import EMPTY_PLAN_HASH, plan_identity
from chemclaw.cli import chat as cli
from chemclaw.core.config import settings
from chemclaw.core.turn_text import get_current_user_texts
from tests.fakes_langgraph import ScriptedChatModel


def test_admin_identity_is_the_configured_actor_holding_the_configured_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admin mode returns the configured actor and exactly `cli_admin_roles` — nothing derived."""
    monkeypatch.setattr(settings, "cli_admin_roles", ["operator"])
    actor, roles = cli.resolve_identity(admin=True, actor=None)
    assert actor == settings.cli_admin_actor
    assert roles == frozenset({"operator"})


def test_admin_holds_no_roles_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--admin` bypasses authentication only. The default confers no entitlement at all."""
    monkeypatch.setattr(settings, "cli_admin_roles", [])
    _actor, roles = cli.resolve_identity(admin=True, actor=None)
    assert roles == frozenset()


def test_a_skill_visibility_gate_cannot_confer_tool_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A skill visibility gate cannot confer tool authorization.

    `skill_role_gates` decides which skills a chemist is shown; `authorize_tool` and
    `authorize_trigger` read `tool_role_gates` and `entra_privileged_role_set`. The CLI identity
    must not derive roles from the skill map, or an ordinary skill-gate configuration would grant
    anyone who can run the console script every tool.
    """
    monkeypatch.setattr(
        settings, "skill_role_gates", {"deep-research": ["process-chemist"], "bo": ["ops"]}
    )
    monkeypatch.setattr(settings, "entra_privileged_roles", "process-chemist")
    monkeypatch.setattr(settings, "cli_admin_roles", [])

    _actor, roles = cli.resolve_identity(admin=True, actor=None)
    assert roles == frozenset(), "a skill-visibility gate must not confer any authz role"
    assert not (roles & settings.entra_privileged_role_set), (
        "the CLI must not hold a privileged role it was never explicitly given"
    )


def test_actor_override_is_honored() -> None:
    """An explicit --actor label overrides the configured default."""
    actor, _ = cli.resolve_identity(admin=True, actor="alice@lab")
    assert actor == "alice@lab"


def test_non_admin_is_refused_until_entra_lands() -> None:
    """Without --admin there is no auth path yet, so the CLI refuses to run."""
    with pytest.raises(SystemExit, match="Entra"):
        cli.resolve_identity(admin=False, actor=None)


def test_message_flag_parses_single_shot() -> None:
    """`-m` captures a one-shot question; --admin/--audit-postgres are flags."""
    args = cli._parse_args(["--admin", "-m", "what is the yield?", "--audit-postgres"])
    assert args.admin is True
    assert args.message == "what is the yield?"
    assert args.audit_postgres is True


def test_converse_returns_the_final_assistant_text() -> None:
    """One turn returns the last assistant message's text (graph path, no LLM).

    The last `AIMessage`, not the tail: a capped turn ends on a `ToolMessage` (see
    `test_a_capped_turn_never_answers_with_a_tool_result`).
    """

    class _Agent:
        async def ainvoke(self, state: dict[str, object], _config: object) -> dict[str, object]:
            assert state["messages"] == [("user", "hi")]
            return {"messages": [AIMessage(content="  55% yield  ")]}

    assert asyncio.run(cli.converse(_Agent(), "hi")).answer.strip() == "55% yield"


def test_a_capped_turn_never_answers_with_a_tool_result() -> None:
    """A capped turn never answers with a tool result: prose, or nothing.

    Both caps jump to `end` from `before_model`, which runs after the tools node, so a capped turn's
    message list ends on a `ToolMessage`.
    """

    class _Agent:
        async def ainvoke(self, state: dict[str, object], _config: object) -> dict[str, object]:
            return {
                "messages": [
                    AIMessage(
                        content="checking the notes",
                        tool_calls=[{"name": "ls", "args": {}, "id": "call-1"}],
                    ),
                    ToolMessage(content="No files found", tool_call_id="call-1"),
                ]
            }

    assert asyncio.run(cli.converse(_Agent(), "hi")).answer == "checking the notes"


def test_successive_turns_continue_one_thread() -> None:
    """`converse` invokes under a stable `thread_id`, which is what makes the CLI multi-turn.

    The checkpointer keys a conversation on that id; a fresh one per turn would forget between
    questions while every single turn still worked.
    """
    seen: list[object] = []

    class _Agent:
        async def ainvoke(self, _state: object, config: dict[str, object]) -> dict[str, object]:
            seen.append(config["configurable"]["thread_id"])  # type: ignore[index]
            return {"messages": [_Message()]}

    class _Message:
        content = "ok"

    agent = _Agent()
    asyncio.run(cli.converse(agent, "first"))
    asyncio.run(cli.converse(agent, "second"))
    assert seen == [cli._CLI_SESSION_ID, cli._CLI_SESSION_ID]


def test_the_repl_carries_what_the_operator_typed_into_the_next_turns_ambient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The REPL carries what the operator typed into the next turn's ambient.

    The front door widens the `basis="stated"` window to the thread's user turns from the
    transcript; the CLI writes none, so the REPL keeps what was typed. Otherwise the two surfaces
    grade an attribution differently. `/plan` and `/approve` are terminal commands and stay out.
    """
    seen: list[tuple[str, ...] | None] = []

    class _Message:
        content = "ok"

    class _Agent:
        async def ainvoke(self, _state: object, _config: object) -> dict[str, object]:
            seen.append(get_current_user_texts())
            return {"messages": [_Message()]}

    typed = iter(["24 wells, no DMF, by Friday please.", "/plan", "ok go ahead", "exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(typed))
    monkeypatch.setattr(cli, "_plan_command", _plan_answer)

    asyncio.run(cli._repl(_Agent(), "admin@localhost", None))

    assert seen == [
        ("24 wells, no DMF, by Friday please.",),
        ("24 wells, no DMF, by Friday please.", "ok go ahead"),
    ]


async def _plan_answer(_prompt: str, _actor: str, _saver: object) -> str:
    """What `/plan` prints, stubbed — the REPL test is about the ambient, not the plan store."""
    return "(no plan yet)"


# --- `/approve` decides on a plan, or refuses — the same question the HTTP route asks -----------


@pytest.fixture
def cli_approvals(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemoryPlanApprovalStore]:
    """The CLI's real approval store, obtained the way `_plan_command` obtains it.

    `session_store="memory"` is the production backend for a terminal. The `@cache`d factory is
    cleared on both sides.
    """
    monkeypatch.setattr(settings, "session_store", "memory")
    factory = store_module.plan_approval_store
    factory.cache_clear()
    store = factory()
    assert isinstance(store, InMemoryPlanApprovalStore)
    yield store
    factory.cache_clear()


@pytest.fixture
def cli_plan(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[str]], None]:
    """Set what the CLI's session is proposing, at the seam `_plan_command` reads it through.

    Reading the plan is tested in `tests/test_plan_state.py`; here the plan is the input. Each step
    declares `record_knowledge_note`, so the approval's scope is non-empty.
    """

    def _set(titles: list[str]) -> None:
        async def _plan(session_id: str, **_kwargs: object) -> list[dict[str, object]]:
            return [
                {"content": t, "status": "pending", "tools": ["record_knowledge_note"]}
                for t in titles
            ]

        monkeypatch.setattr(plan_state, "session_plan", _plan)

    return _set


def test_approve_refuses_a_session_with_no_plan(
    cli_approvals: InMemoryPlanApprovalStore, cli_plan: Callable[[list[str]], None]
) -> None:
    """`/approve` refuses a session with no plan.

    An empty todo list hashes to `EMPTY_PLAN_HASH`, shared by every session; recording against it is
    meaningless, and the terminal must not claim the session may now execute.
    """
    cli_plan([])

    async def _run() -> tuple[str, Decision | None]:
        reply = await cli._plan_command("/approve", settings.cli_admin_actor, saver=None)
        return reply, await cli_approvals.decision(cli._CLI_SESSION_ID, EMPTY_PLAN_HASH)

    reply, recorded = asyncio.run(_run())
    assert "no plan to approve" in reply, f"a planless session was told it approved: {reply}"
    assert recorded is None, "an approval was recorded against the empty-plan constant"


def test_approve_records_and_arms_a_real_plan(
    cli_approvals: InMemoryPlanApprovalStore, cli_plan: Callable[[list[str]], None]
) -> None:
    """The counterweight: a session proposing real work items is approvable and says so.

    A refusal that refused everything would be a broken command rather than a fixed one.
    """
    titles = ["screen the species", "compute the barrier"]
    cli_plan(titles)
    # The steps in `session_plan`'s shape: the plan identity covers each step's declared tools as
    # well as its content, so a hash over titles alone would never match.
    steps = [
        {"content": title, "status": "pending", "tools": ["record_knowledge_note"]}
        for title in titles
    ]

    async def _run() -> tuple[str, str, Decision | None]:
        reply = await cli._plan_command("/approve", "alice@lab", saver=None)
        plan_hash = plan_identity(steps) or EMPTY_PLAN_HASH
        return reply, plan_hash, await cli_approvals.decision(cli._CLI_SESSION_ID, plan_hash)

    reply, plan_hash, recorded = asyncio.run(_run())
    assert plan_hash != EMPTY_PLAN_HASH, "the precondition is a plan with real work items"
    assert plan_hash in reply, f"the terminal did not name the plan it approved: {reply}"
    # The session's actor, not `settings.cli_admin_actor`, so the approval record agrees with the
    # session's audit rows.
    assert recorded is not None and (recorded.approved, recorded.actor) == (True, "alice@lab")
    # And the approval carries what the plan's steps declared: the gate reads this column rather
    # than the live todo list, so a decision recorded with an empty scope authorizes nothing
    # (`D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool`).
    assert recorded.scope == frozenset({"record_knowledge_note"}), (
        f"the terminal recorded an approval that authorizes {sorted(recorded.scope)}"
    )


def test_plan_shows_no_approvable_identity_rather_than_the_empty_constant(
    cli_approvals: InMemoryPlanApprovalStore, cli_plan: Callable[[list[str]], None]
) -> None:
    """`/plan` reports an identity only when there is one to report.

    Printing `EMPTY_PLAN_HASH` beside a verdict invited exactly the confusion `/approve` acted on:
    it looks like a plan identity, and it is a global constant.
    """
    cli_plan([])

    async def _run() -> str:
        return await cli._plan_command("/plan", settings.cli_admin_actor, saver=None)

    reply = asyncio.run(_run())
    assert "no approvable plan" in reply, f"the empty constant was shown as a plan: {reply}"
    assert EMPTY_PLAN_HASH not in reply


# --- The checkpointer the CLI documented and did not have -------------------------------------


async def test_a_second_turn_continues_the_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second CLI turn continues the first.

    Asserted on what the model is handed on the second turn, because a graph without a checkpointer
    answers both turns without complaint.
    """
    monkeypatch.setattr(settings, "session_store", "memory")

    class _Recording(GenericFakeChatModel):
        seen: list[list[Any]] = []

        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            return self

        def _generate(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
            type(self).seen.append(list(messages))
            return super()._generate(messages, *args, **kwargs)

    _Recording.seen = []

    saver = await process_checkpointer()
    agent = build_langgraph_agent(
        model=_Recording(messages=iter([AIMessage(content="one"), AIMessage(content="two")])),
        checkpointer=saver,
    )
    await cli.converse(agent, "first question")
    await cli.converse(agent, "second question")

    second = _Recording.seen[1]
    assert any("first question" in str(m.content) for m in second), (
        "the second turn did not see the first; the CLI is not a conversation"
    )


def test_the_plan_command_reads_the_store_the_turns_wrote_to(
    monkeypatch: pytest.MonkeyPatch, cli_approvals: InMemoryPlanApprovalStore
) -> None:
    """`/plan` shows the plan the session actually proposed, from the saver the turns wrote to.

    `plan_state.session_todos` is not stubbed here: the real function reads a checkpointer, so the
    test threads one saver to both the graph and `/plan` exactly as `_run` does.
    """
    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "entra_required", False)
    plan = "screen three solvents"

    async def _run() -> str:
        saver = await process_checkpointer()
        model = _WriteTodosThenAnswer(
            messages=iter(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "write_todos",
                                "args": {
                                    "todos": [
                                        # `tools` is required: a step declares what it will call
                                        # and the approval is scoped to the union
                                        # (`agent/plan_scope.py`).
                                        {
                                            "content": plan,
                                            "status": "pending",
                                            "tools": ["record_knowledge_note"],
                                        }
                                    ]
                                },
                                "id": "call-1",
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            )
        )
        agent = build_langgraph_agent(model=model, checkpointer=saver)
        await cli.converse(agent, "what should we try?")
        return await cli._plan_command("/plan", settings.cli_admin_actor, saver)

    reply = asyncio.run(_run())

    assert plan in reply, f"/plan did not show the plan the turn proposed: {reply!r}"
    assert "(no plan yet)" not in reply
    # And what approving it would authorize, because that is half of what the person is deciding.
    assert "declares: record_knowledge_note" in reply, (
        f"/plan showed the steps without what they declared: {reply!r}"
    )


class _WriteTodosThenAnswer(GenericFakeChatModel):
    """A model that writes a plan and then answers — the shape a harness turn actually takes."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding; the script already names the call."""
        return self


def test_a_startup_failure_is_a_message_and_an_exit_code_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A startup failure is a message and an exit code, not a traceback.

    A missing credential, an unreachable checkpointer DSN or a blank `CHEMCLAW_LLM_BASE_URL` must
    reach the operator as one sentence naming the fix.
    """

    def _fails(_args: object) -> None:
        raise RuntimeError("the LLM gateway requires llm_base_url to be set")

    monkeypatch.setattr(cli, "_run", _fails)
    assert cli.main(["--admin", "-m", "hello"]) == 1
    assert "the LLM gateway requires llm_base_url" in capsys.readouterr().err


def test_the_console_script_returns_an_exit_code_on_the_happy_path_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`main` hands an exit code to the console script, not `None`, in both directions.

    The console-script wrapper turns the return value into the process status. Asserted beside the
    failure case so the error path cannot be satisfied by returning 1 always. `main` passes `_run`'s
    status through, which makes the degraded exit reachable.
    """

    async def _clean(_args: object) -> int:
        return 0

    async def _degraded(_args: object) -> int:
        return cli._DEGRADED_EXIT

    monkeypatch.setattr(cli, "_run", _clean)
    assert cli.main(["--admin", "-m", "hello"]) == 0
    monkeypatch.setattr(cli, "_run", _degraded)
    assert cli.main(["--admin", "-m", "hello"]) == cli._DEGRADED_EXIT


# --- a capped turn stops printing as a finished one ----------------------------------------------


def _capped_script(first: str) -> Iterator[AIMessage]:
    """A model that keeps calling `ls` and only eventually answers.

    `first` is the content of the tool-calling turns, so the same script covers both shapes the
    cap can leave behind: a partial sentence, and nothing at all.
    """
    usage = {"input_tokens": 50, "output_tokens": 10, "total_tokens": 60, "input_token_details": {}}
    calls = [
        AIMessage(
            content=first,
            tool_calls=[{"name": "ls", "args": {"path": "."}, "id": f"c{index}"}],
            usage_metadata=usage,
        )
        for index in range(6)
    ]
    answer = "FINAL: pKa 3.49, confirmed against ELN batch 12."
    done = AIMessage(content=answer, usage_metadata=usage)
    return iter([*calls, *([done] * 41)])


def _cli_turn(
    monkeypatch: pytest.MonkeyPatch, cap: int, first: str = "Still checking; one more source."
) -> cli.CliTurn:
    """Drive `converse` on a real compiled graph at `cap`, and report the turn.

    Only a compiled graph proves the cap decision is wired; its flag lives on an untracked channel
    that only a real run sets.
    """
    monkeypatch.setattr(settings, "harness_max_loop_iterations", cap)
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 0)
    model = ScriptedChatModel(messages=_capped_script(first))
    monkeypatch.setattr("chemclaw.agent.langgraph_agent.build_chat_model", lambda *_a, **_k: model)
    agent = build_langgraph_agent(actor="chemist-1")
    return asyncio.run(cli.converse(agent, "what is the pKa of CCO?", session_id=f"cli-{cap}"))


def test_a_capped_cli_turn_says_so_and_a_whole_one_says_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capped CLI turn says so and a whole one says nothing, both on a real graph.

    The control arm stops a notice on every turn from satisfying the first assertion.
    """
    capped = _cli_turn(monkeypatch, 2)
    assert capped.answer == "Still checking; one more source."
    assert capped.notice == "incomplete: the turn reached its model-call cap before it finished"

    whole = _cli_turn(monkeypatch, 20)
    assert whole.answer == "FINAL: pKa 3.49, confirmed against ELN batch 12."
    assert whole.notice == ""


def test_a_turn_that_answered_nothing_is_not_a_blank_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At a cap of 1 over a silent first turn the CLI printed `''` and exited 0.

    The cap outranks the empty answer, which is `api/runner._settle_outcome`'s ranking rather than
    a second one: a turn stopped by its cap is a capped turn whether or not it managed prose.
    """
    turn = _cli_turn(monkeypatch, 1, first="")

    assert turn.answer == ""
    assert turn.notice == "incomplete: the turn reached its model-call cap before it finished"


def test_the_notice_names_the_spend_cap_and_the_silent_turn_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The notice tells the spend cap and the silent turn apart.

    Driven on states rather than a graph: the readers are under test, and the graph arm above
    already proves the wiring.
    """
    assert cli.turn_notice({"spend_capped": True}, "partial") == (
        "incomplete: the turn reached its token budget before it finished"
    )
    assert cli.turn_notice({}, "") == "incomplete: the turn produced no answer"
    assert cli.turn_notice({}, "a whole answer") == ""


def test_a_one_shot_run_exits_nonzero_when_the_answer_it_printed_is_incomplete(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A one-shot run exits non-zero when the answer it printed is incomplete.

    stdout keeps the answer (delivered, marked), the reason goes to stderr, and the status is
    `_DEGRADED_EXIT`, distinct from success and from a refused startup.
    """
    monkeypatch.setattr(cli, "resolve_identity", lambda **_k: ("admin@localhost", frozenset()))
    monkeypatch.setattr(cli, "process_checkpointer", _none)
    monkeypatch.setattr(cli, "open_connector_specs", _no_connectors)
    monkeypatch.setattr(cli, "_build_cli_agent", lambda *_a, **_k: object())

    async def _capped(_agent: object, _prompt: str, **_kwargs: object) -> cli.CliTurn:
        return cli.CliTurn("  Still checking; one more source.  ", "incomplete: capped")

    monkeypatch.setattr(cli, "converse", _capped)
    code = asyncio.run(cli._run(cli._parse_args(["--admin", "-m", "pKa of CCO?"])))
    captured = capsys.readouterr()

    assert code == cli._DEGRADED_EXIT
    assert captured.out == "Still checking; one more source.\n"
    assert "warning: incomplete: capped" in captured.err

    async def _whole(_agent: object, _prompt: str, **_kwargs: object) -> cli.CliTurn:
        return cli.CliTurn("FINAL: pKa 3.49.", "")

    monkeypatch.setattr(cli, "converse", _whole)
    assert asyncio.run(cli._run(cli._parse_args(["--admin", "-m", "pKa of CCO?"]))) == 0


async def _none() -> None:
    """No checkpointer — this CLI run takes one turn against a stand-in agent."""
    return None


async def _no_connectors(_stack: object, _specs: object) -> tuple[list[Any], list[str]]:
    """No MCP subprocesses, and none reported unreachable: the notice under test is the turn's."""
    return [], []
