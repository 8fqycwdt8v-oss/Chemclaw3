"""Terminal CLI for driving the Chemclaw agent locally — the testing front door.

Why this exists: the production ingress is Teams/Copilot Studio with native Entra-ID SSO
(architektur.md §7), so day-to-day there is no way to actually *talk* to the wired agent from a
checkout. This CLI is that seam for development and testing: it builds the same `build_agent`
the production host builds, opens the MCP capability subprocesses for the session, and runs a
turn-taking chat (or a single scripted question) against a live model.

Identity is the one thing that differs from production. Entra-ID auth (F4, D-043) is a front-door
OIDC flow — it validates a browser-obtained token — and this is a terminal tool with no such token
to resolve a real principal from. Rather than pretend, the CLI runs only in explicit **admin mode**
(`--admin`): it bypasses auth and stamps the ambient identity (`agents.identity_context`, same seam
the front door stamps per turn) with the configured admin actor (`settings.cli_admin_actor`) and
every role named in `settings.skill_role_gates`, so admin keeps seeing every skill regardless of how
gates are configured. `resolve_identity` is the seam where a non-admin branch could resolve identity
from some other token source in the future; today that branch fails loudly rather than silently
running unauthenticated. Requiring the flag keeps "no authentication" a conscious choice, not a
default — the GxP posture, in a dev tool.

Run: `make chat`, `uv run chemclaw --admin`, or one-shot `uv run chemclaw --admin -m "…"`.
"""

import argparse
import asyncio
import contextlib
import sys
from collections.abc import Sequence
from typing import Any

from agents.audit import AuditSink
from agents.audit_store import PostgresAuditSink
from agents.chemclaw_agent import build_agent
from agents.identity_context import reset_current_identity, set_current_identity
from chemclaw.config import settings
from chemclaw.logging import configure_logging

_EXIT_WORDS = {"exit", "quit", ":q"}


def resolve_identity(*, admin: bool, actor: str | None) -> tuple[str, frozenset[str]]:
    """Resolve the caller's audit actor and ambient roles — the CLI's identity seam.

    Returns `(actor, roles)`, stamped as the ambient identity for the whole CLI session so audit
    attribution, the authorization gate, and role-scoped skill visibility all see it (F4). This
    CLI has no browser OIDC token to validate, so it runs only in admin mode, holding every role
    named in `settings.skill_role_gates` — preserving the CLI's promise of advertising every skill
    regardless of how gates are configured.

    Args:
        admin: Run in admin testing mode, bypassing Entra auth (this CLI has no token to check).
        actor: Override the audit actor label; defaults to `settings.cli_admin_actor`.
    """
    if not admin:
        raise SystemExit(
            "This CLI has no Entra-ID token to authenticate with (it is a terminal tool, not "
            "the front-door OIDC flow). Re-run with --admin to use the CLI unauthenticated for "
            "testing (bypasses auth; advertises all skills)."
        )
    admin_roles = frozenset(role for roles in settings.skill_role_gates.values() for role in roles)
    return actor or settings.cli_admin_actor, admin_roles


def _build_cli_agent(args: argparse.Namespace, actor: str) -> Any:
    """Build the agent for a CLI session from parsed args and the resolved build-time actor.

    `actor` is only the build-time audit fallback (used if a code path runs outside the ambient
    identity `_run` stamps for the session, e.g. a background task); the ambient identity is what
    audit/authz/skill-scoping actually read at call time. The default chat client reads
    `ANTHROPIC_API_KEY` at construction and fails with a clear message if it is missing (D-037),
    so a credential problem surfaces here, before the prompt.
    """
    sink: AuditSink | None = PostgresAuditSink() if args.audit_postgres else None
    return build_agent(actor=actor, audit_sink=sink)


async def converse(agent: Any, prompt: str) -> str:
    """Run one turn against the agent and return its text answer.

    The agent's session history provider accumulates the thread across calls, so reusing the same
    `agent` object over successive `converse` calls is a multi-turn conversation (no session
    plumbing needed here). MCP contexts must already be open (see `_run`).
    """
    response = await agent.run(prompt)
    return str(response.text)


async def _run(args: argparse.Namespace) -> None:
    """Resolve identity, build the agent, open its MCP subprocesses, and dispatch.

    Identity is stamped ambient (`agents.identity_context`) for the whole session — a CLI run is
    one actor throughout, unlike the multi-user front door, which stamps it per turn (F2/F4) —
    and reset on exit. The MCP capability servers are spawned once for the session and torn down
    on exit (the `async with` over `agent.mcp_tools` the `build_agent` docstring prescribes), so a
    multi-turn REPL does not re-launch them per turn.
    """
    actor, roles = resolve_identity(admin=args.admin, actor=args.actor)
    identity_token = set_current_identity(actor, roles)
    try:
        agent = _build_cli_agent(args, actor)
        async with contextlib.AsyncExitStack() as stack:
            for tool in agent.mcp_tools:
                await stack.enter_async_context(tool)
            if args.message is not None:
                print((await converse(agent, args.message)).strip())
            else:
                await _repl(agent)
    finally:
        reset_current_identity(identity_token)


async def _repl(agent: Any) -> None:
    """Read a question, print the answer, repeat — until EOF, Ctrl-C, or an exit word.

    Prompts/errors go to stderr so a redirected stdout carries only the answers.
    """
    print("Chemclaw CLI — type a question, or 'exit' to quit.", file=sys.stderr)
    while True:
        try:
            prompt = input("chemclaw> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return
        if not prompt:
            continue
        if prompt.lower() in _EXIT_WORDS:
            return
        try:
            print((await converse(agent, prompt)).strip())
        except Exception as exc:  # keep the session alive across a single failed turn
            print(f"error: {exc}", file=sys.stderr)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the CLI arguments."""
    parser = argparse.ArgumentParser(
        prog="chemclaw",
        description="Chat with the Chemclaw agent from the terminal (testing front door).",
    )
    parser.add_argument(
        "--admin",
        action="store_true",
        help="Run unauthenticated as the admin actor (bypasses Entra auth; advertises all "
        "skills). Required — this terminal tool has no front-door OIDC token to check.",
    )
    parser.add_argument(
        "--actor",
        default=None,
        help=f"Audit-trail actor label (default: {settings.cli_admin_actor!r}).",
    )
    parser.add_argument(
        "-m",
        "--message",
        default=None,
        help="Ask one question and exit (scriptable), instead of the interactive REPL.",
    )
    parser.add_argument(
        "--audit-postgres",
        action="store_true",
        help="Persist the tool-audit trail to Postgres (default: log-only).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entrypoint (`chemclaw` console script / `python -m agents.cli`)."""
    configure_logging()
    asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    main()
