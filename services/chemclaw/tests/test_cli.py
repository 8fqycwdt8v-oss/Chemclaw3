"""The testing CLI resolves identity, parses args, and runs a turn (agents/cli.py).

Credential-free: identity/arg logic is pure, and the run path is exercised with a stub agent so
no LLM or MCP subprocess is needed — this proves the CLI plumbing (admin-only auth gate, actor
resolution, single-turn text extraction), not model behavior.
"""

import asyncio

import pytest

from agents import cli
from chemclaw.config import settings


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

        async def run(self, prompt: str) -> _Response:
            assert prompt == "hi"
            return _Response()

    assert asyncio.run(cli.converse(_Agent(), "hi")).strip() == "55% yield"
