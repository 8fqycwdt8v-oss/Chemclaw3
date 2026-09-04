"""The testing CLI resolves identity, parses args, and runs a turn (chemclaw/cli/chat.py).

Credential-free: identity/arg logic is pure, and the run path is exercised with a stub agent so
no LLM or MCP subprocess is needed — this proves the CLI plumbing (admin-only auth gate, actor
resolution, single-turn text extraction), not model behavior.
"""

import asyncio
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from chemclaw.agent import plan_approval_store as store_module
from chemclaw.agent import plan_state
from chemclaw.agent.checkpointer import process_checkpointer
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.plan_approval_store import InMemoryPlanApprovalStore
from chemclaw.agent.plan_gate import EMPTY_PLAN_HASH, plan_identity
from chemclaw.cli import chat as cli
from chemclaw.core.config import settings
from chemclaw.core.turn_text import get_current_user_texts


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
    """The coupling that made an unauthenticated terminal fully privileged.

    `resolve_identity` used to hold the union of every role named in `skill_role_gates` — a map that
    decides which *skills a chemist is shown*. `authorize_tool` and `authorize_trigger` read
    `tool_role_gates` and `entra_privileged_role_set`. Unrelated maps, coupled by nothing but the
    role *name*.

    On the shipped chart the derivation was harmless. But `core/config/agent.py`'s own docstring
    gives `{"deep-research": ["process-chemist"]}` as the skill-gate example, and the runbook's
    remedy for a refused expensive job is to put a role in `entra_privileged_roles` — so an operator
    following both, in two edits neither of which mentions the CLI, handed every tool and every
    expensive action to anyone who could run the console script. `uv sync` installs it into the
    image, so that is anyone who can `oc exec` into a pod.

    This asserts the two maps are now independent: the exact configuration that opened everything
    now confers nothing.
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
    """One turn returns the last message's text (graph path, no LLM).

    The answer is the *last* message rather than a single `response.text`, because a graph returns
    its whole message list — so a turn that called tools ends with the model's reply after them,
    and reading anything but the tail would surface a tool result as the answer.
    """

    class _Message:
        content = "  55% yield  "

    class _Agent:
        async def ainvoke(self, state: dict[str, object], _config: object) -> dict[str, object]:
            assert state["messages"] == [("user", "hi")]
            return {"messages": [_Message()]}

    assert asyncio.run(cli.converse(_Agent(), "hi")).strip() == "55% yield"


def test_successive_turns_continue_one_thread() -> None:
    """`converse` invokes under a stable `thread_id`, which is what makes the CLI multi-turn.

    The checkpointer keys a conversation on that id, so passing a different one per turn would
    give a terminal session amnesia between questions while every individual turn still worked.

    Its ancestor asserted that `converse` passed a `session=` to `agent.run`, because under
    `harness_enabled` MAF's `ToolApprovalMiddleware` raised "requires an TurnSession" on a
    session-less run and the CLI could not take a single turn under the shipped Helm configuration
    (D-152). A thread id is a string in a config dict; there is nothing to be absent, so what is
    left worth pinning is that it does not *change*.
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
    """The CLI's half of the `basis="stated"` window, and it is a bypass if it differs.

    The front door widens the quotable ambient to the thread's user turns by reading the session
    transcript (`api.runner._earlier_user_texts`). This CLI writes no transcript, so its window is
    the process: the REPL keeps what has been typed and hands it to `converse`. A terminal on which
    a chemist's constraint from two prompts ago is unquotable, while the same conversation through
    the front door accepts it, is one surface grading an attribution differently from the other.

    The two operator commands stay out of it: `/plan` and `/approve` are instructions to the
    terminal, and a `stated` slot quoting `'/approve'` would record a UI action as something a
    chemist said.
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

    `session_store="memory"` is what a terminal is, so this is the production backend for this
    front door rather than a double. The `@cache`d factory is cleared on both sides so the store is
    neither inherited nor left behind.
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

    The plan lives in the checkpointer now, and reading it is `agent/plan_state.session_todos`'s
    job — tested against a real one in `tests/test_plan_state.py`. What these tests are about is
    what `/plan` and `/approve` *decide* given a plan, so the read is the input, not the subject.
    """

    def _set(titles: list[str]) -> None:
        async def _todos(session_id: str, **_kwargs: object) -> list[str]:
            return list(titles)

        monkeypatch.setattr(plan_state, "session_todos", _todos)

    return _set


def test_approve_refuses_a_session_with_no_plan(
    cli_approvals: InMemoryPlanApprovalStore, cli_plan: Callable[[list[str]], None]
) -> None:
    """`/approve` must decide on a *plan*, and an empty todo list is not one.

    The empty list hashes to `EMPTY_PLAN_HASH`, a constant every session in every deployment
    proposes whenever it holds no todos — so a decision recorded against it means nothing, and the
    gate refuses that identity anyway. Nothing unsafe followed when this was missing, but the
    terminal answered "approved …; the session may now execute" when nothing had been approved and
    the session could not execute, which is the one thing an approval prompt must never say.

    Its ancestor made the same point about a session holding *only* the launcher's `awaiting-job:`
    bookkeeping rows, which the display counted and the hash stripped — two questions, one guard.
    That distinction is structural now rather than a parse: nothing writes job bookkeeping into
    `todos` at all (a launched job is a `job_records` row and a `session_events` push-back), so such
    a session is simply this one.
    """
    cli_plan([])

    async def _run() -> tuple[str, tuple[bool, str] | None]:
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

    async def _run() -> tuple[str, str, tuple[bool, str] | None]:
        reply = await cli._plan_command("/approve", "alice@lab", saver=None)
        plan_hash = plan_identity(titles) or EMPTY_PLAN_HASH
        return reply, plan_hash, await cli_approvals.decision(cli._CLI_SESSION_ID, plan_hash)

    reply, plan_hash, recorded = asyncio.run(_run())
    assert plan_hash != EMPTY_PLAN_HASH, "the precondition is a plan with real work items"
    assert plan_hash in reply, f"the terminal did not name the plan it approved: {reply}"
    # The *session's* actor, not `settings.cli_admin_actor`. `--actor alice@lab` stamps the ambient
    # identity every audit row and `requested_by` reads, and the approval used to hardcode the
    # default instead — so the durable record of a sign-off named someone who took no action and
    # disagreed with the audit rows for its own session.
    assert recorded == (True, "alice@lab")


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


def test_a_second_turn_continues_the_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI is a multi-turn conversation, which it was not.

    `converse` documented that reusing one `session_id` "continues the thread the last one left",
    and `_build_cli_agent` passed no checkpointer at all, so every turn began from an empty thread.
    Asserted against what the *model* was handed on the second turn, because that is the only place
    the difference is visible — a graph with no checkpointer answers both turns quite happily.
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

    async def _run() -> None:
        saver = await process_checkpointer()
        agent = build_langgraph_agent(
            model=_Recording(messages=iter([AIMessage(content="one"), AIMessage(content="two")])),
            checkpointer=saver,
        )
        await cli.converse(agent, "first question")
        await cli.converse(agent, "second question")

    asyncio.run(_run())

    second = _Recording.seen[1]
    assert any("first question" in str(m.content) for m in second), (
        "the second turn did not see the first; the CLI is not a conversation"
    )


def test_the_plan_command_reads_the_store_the_turns_wrote_to(
    monkeypatch: pytest.MonkeyPatch, cli_approvals: InMemoryPlanApprovalStore
) -> None:
    """`/plan` shows the plan the session actually proposed — the defect the saver thread fixes.

    Deliberately **not** stubbing `plan_state.session_todos`, which is what every other test in this
    file does and what made the defect invisible: the real function resolves *the configured*
    checkpointer, so with the graph built on a different one (or on none) `/plan` answered
    "(no plan yet)" for every session under every configuration, and the harness this CLI exists to
    exercise could not be exercised. One saver, handed to both, is the whole fix — so the test hands
    it to neither and threads it exactly as `_run` does.
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
                                "args": {"todos": [{"content": plan, "status": "pending"}]},
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


class _WriteTodosThenAnswer(GenericFakeChatModel):
    """A model that writes a plan and then answers — the shape a harness turn actually takes."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding; the script already names the call."""
        return self


def test_a_startup_failure_is_a_message_and_an_exit_code_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The single most likely first run of the only interactive entrypoint, answered properly.

    `_build_cli_agent`'s docstring promises the chat model "fails with a clear message if it is
    missing (D-037), so a credential problem surfaces here, before the prompt". The message is
    good; `main` had no exception handling and returned `None`, so it arrived under an asyncio and
    graph-construction stack trace — and the console script exited on the traceback rather than on
    a code. A new operator running `make chat` without a credential got nine frames instead of the
    one sentence naming what to export, and the same hole covered every other startup failure: an
    unreachable checkpointer DSN, a bad `CHEMCLAW_LLM_PROVIDER`.
    """

    def _fails(_args: object) -> None:
        raise RuntimeError("ANTHROPIC_API_KEY is not set — the Anthropic chat-client path needs it")

    monkeypatch.setattr(cli, "_run", _fails)
    assert cli.main(["--admin", "-m", "hello"]) == 1
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err


def test_the_console_script_returns_an_exit_code_on_the_happy_path_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`main` hands an exit code to the console script, not `None`, in both directions.

    `[project.scripts] chemclaw = "chemclaw.cli.chat:main"` — the console-script wrapper turns
    whatever `main` returns into the process status, so a `-> None` entrypoint always exited 0 and
    a caller had no way to tell a refused startup from an answered question except by reading the
    traceback. Asserted beside the failure case so the error path cannot be satisfied by returning
    1 unconditionally.
    """
    monkeypatch.setattr(cli, "_run", lambda _args: asyncio.sleep(0))
    assert cli.main(["--admin", "-m", "hello"]) == 0
