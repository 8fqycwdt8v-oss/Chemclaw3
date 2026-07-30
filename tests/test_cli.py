"""The testing CLI resolves identity, parses args, and runs a turn (chemclaw/cli/chat.py).

Credential-free: identity/arg logic is pure, and the run path is exercised with a stub agent so
no LLM or MCP subprocess is needed — this proves the CLI plumbing (admin-only auth gate, actor
resolution, single-turn text extraction), not model behavior.
"""

import asyncio

import pytest

from chemclaw.cli import chat as cli
from chemclaw.core.config import settings


def test_admin_identity_advertises_all_skills_as_the_configured_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admin mode returns the configured actor and holds every role any skill gate requires."""
    monkeypatch.setattr(
        settings, "skill_role_gates", {"deep-research": ["process-chemist"], "bo": ["ops"]}
    )
    actor, roles = cli.resolve_identity(admin=True, actor=None)
    assert actor == settings.cli_admin_actor
    assert roles == frozenset({"process-chemist", "ops"})


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
