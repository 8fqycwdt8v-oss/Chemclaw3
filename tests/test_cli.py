"""The testing CLI resolves identity, parses args, and runs a turn (chemclaw/cli/chat.py).

Credential-free: identity/arg logic is pure, and the run path is exercised with a stub agent so
no LLM or MCP subprocess is needed — this proves the CLI plumbing (admin-only auth gate, actor
resolution, single-turn text extraction), not model behavior.
"""

import asyncio
from collections.abc import Iterator

import pytest
from agent_framework import DEFAULT_TODO_SOURCE_ID, AgentSession, TodoItem, TodoSessionStore

from chemclaw.agent import plan_approval_store as store_module
from chemclaw.agent.harness_mode import EMPTY_PLAN_HASH, current_plan_hash
from chemclaw.agent.harness_todo import mark_awaiting_job, todo_titles
from chemclaw.agent.plan_approval_store import InMemoryPlanApprovalStore
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


def test_converse_returns_the_agent_text() -> None:
    """One turn returns the agent response's text (run path, no LLM)."""

    class _Response:
        text = "  55% yield  "

    class _Agent:
        mcp_tools: list[object] = []

        async def run(self, prompt: str, **_run_options: object) -> _Response:
            assert prompt == "hi"
            return _Response()

    assert asyncio.run(cli.converse(_Agent(), "hi")).strip() == "55% yield"


def test_a_turn_runs_on_a_session_because_the_harness_requires_one() -> None:
    """`converse` passes its session to `agent.run` (D-152).

    Not cosmetic: under `harness_enabled` — which the shipped Helm chart sets — MAF's
    `ToolApprovalMiddleware` raises `requires an AgentSession` on a session-less `agent.run`, so
    the CLI could not take a single turn under the production configuration. The front door always
    passed a session and never met it. Fails on the unfixed code, where `session` never reached
    `run`.
    """

    class _Response:
        text = "ok"

    class _Agent:
        mcp_tools: list[object] = []

        def __init__(self) -> None:
            self.seen: object = "never called"

        async def run(self, prompt: str, **run_options: object) -> _Response:
            self.seen = run_options.get("session")
            return _Response()

    agent = _Agent()
    sentinel = object()
    asyncio.run(cli.converse(agent, "hi", (), sentinel))
    assert agent.seen is sentinel


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


async def _set_plan(session: AgentSession, titles: list[str]) -> None:
    """Write `titles` as the session's plan, the way the model's own todo tool does."""
    items = [TodoItem(id=index + 1, title=title) for index, title in enumerate(titles)]
    await TodoSessionStore().save_state(
        session, items, next_id=len(items) + 1, source_id=DEFAULT_TODO_SOURCE_ID
    )


def test_approve_refuses_a_session_whose_todos_are_only_bookkeeping(
    cli_approvals: InMemoryPlanApprovalStore,
) -> None:
    """`/approve` must decide on a *plan*, not on the launcher's `awaiting-job:` rows.

    The guard used to be `todo_titles`, which is the *display* list and counts those rows, while
    the hash recorded was `current_plan_hash`, which strips them and falls back to the global
    `EMPTY_PLAN_HASH`. Two different questions, so a session holding nothing but bookkeeping passed
    the guard and recorded an approval against a constant every session in every deployment shares.
    The gate refuses that identity, so nothing unsafe followed — but the terminal answered
    "approved …; the session may now execute" when nothing had been approved and the session could
    not execute, which is the one thing an approval prompt must never say.
    """

    async def _run() -> tuple[str, tuple[bool, str] | None]:
        session = AgentSession(session_id="cli-bookkeeping")
        await mark_awaiting_job(session, "job-1", title="waiting on the DFT run")
        assert await todo_titles(session), "the precondition is that the display is non-empty"
        reply = await cli._plan_command("/approve", session, settings.cli_admin_actor)
        return reply, await cli_approvals.decision(session.session_id, EMPTY_PLAN_HASH)

    reply, recorded = asyncio.run(_run())
    assert "no plan to approve" in reply, (
        f"a bookkeeping-only session was told it approved: {reply}"
    )
    assert recorded is None, "an approval was recorded against the empty-plan constant"


def test_approve_records_and_arms_a_real_plan(cli_approvals: InMemoryPlanApprovalStore) -> None:
    """The counterweight: a session proposing real work items is approvable and says so.

    A refusal that refused everything would be a broken command rather than a fixed one.
    """

    async def _run() -> tuple[str, str, tuple[bool, str] | None]:
        session = AgentSession(session_id="cli-real-plan")
        await _set_plan(session, ["screen the species", "compute the barrier"])
        reply = await cli._plan_command("/approve", session, "alice@lab")
        plan_hash = await current_plan_hash(session)
        return reply, plan_hash, await cli_approvals.decision(session.session_id, plan_hash)

    reply, plan_hash, recorded = asyncio.run(_run())
    assert plan_hash != EMPTY_PLAN_HASH, "the precondition is a plan with real work items"
    assert plan_hash in reply, f"the terminal did not name the plan it approved: {reply}"
    # The *session's* actor, not `settings.cli_admin_actor`. `--actor alice@lab` stamps the ambient
    # identity every audit row and `requested_by` reads, and the approval used to hardcode the
    # default instead — so the durable record of a GxP sign-off named someone who took no action and
    # disagreed with the audit rows for its own session.
    assert recorded == (True, "alice@lab")


def test_plan_shows_no_approvable_identity_rather_than_the_empty_constant(
    cli_approvals: InMemoryPlanApprovalStore,
) -> None:
    """`/plan` displays what the chemist sees, and reports the identity only when there is one.

    Printing `EMPTY_PLAN_HASH` beside a verdict invited exactly the confusion `/approve` acted on:
    it looks like a plan identity, and it is a global constant. The todo lines are still shown in
    full — the bookkeeping rows are what the session is genuinely doing — because emptiness
    invalidates *deciding*, not displaying.
    """

    async def _run() -> str:
        session = AgentSession(session_id="cli-display")
        await mark_awaiting_job(session, "job-1", title="waiting on the DFT run")
        return await cli._plan_command("/plan", session, settings.cli_admin_actor)

    reply = asyncio.run(_run())
    assert "waiting on the DFT run" in reply, f"the display lost the session's todos: {reply}"
    assert "no approvable plan" in reply, f"the empty constant was shown as a plan: {reply}"
    assert EMPTY_PLAN_HASH not in reply
