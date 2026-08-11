"""The testing CLI resolves identity, parses args, and runs a turn (chemclaw/cli/chat.py).

Credential-free: identity/arg logic is pure, and the run path is exercised with a stub agent so
no LLM or MCP subprocess is needed — this proves the CLI plumbing (admin-only auth gate, actor
resolution, single-turn text extraction), not model behavior.
"""

import asyncio
from collections.abc import Callable, Iterator

import pytest

from chemclaw.agent import plan_approval_store as store_module
from chemclaw.agent import plan_state
from chemclaw.agent.plan_approval_store import InMemoryPlanApprovalStore
from chemclaw.agent.plan_gate import EMPTY_PLAN_HASH, plan_identity
from chemclaw.cli import chat as cli
from chemclaw.core.config import settings


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
    That distinction is structural now rather than a parse: waiting jobs are a separate state field
    (`agent/state.ChemclawState.awaiting_jobs`), so they are not in `todos` and such a session is
    simply this one.
    """
    cli_plan([])

    async def _run() -> tuple[str, tuple[bool, str] | None]:
        reply = await cli._plan_command("/approve", settings.cli_admin_actor)
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
        reply = await cli._plan_command("/approve", "alice@lab")
        plan_hash = plan_identity(titles) or EMPTY_PLAN_HASH
        return reply, plan_hash, await cli_approvals.decision(cli._CLI_SESSION_ID, plan_hash)

    reply, plan_hash, recorded = asyncio.run(_run())
    assert plan_hash != EMPTY_PLAN_HASH, "the precondition is a plan with real work items"
    assert plan_hash in reply, f"the terminal did not name the plan it approved: {reply}"
    # The *session's* actor, not `settings.cli_admin_actor`. `--actor alice@lab` stamps the ambient
    # identity every audit row and `requested_by` reads, and the approval used to hardcode the
    # default instead — so the durable record of a GxP sign-off named someone who took no action and
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
        return await cli._plan_command("/plan", settings.cli_admin_actor)

    reply = asyncio.run(_run())
    assert "no approvable plan" in reply, f"the empty constant was shown as a plan: {reply}"
    assert EMPTY_PLAN_HASH not in reply
