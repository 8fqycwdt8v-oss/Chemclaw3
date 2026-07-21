"""Terminal CLI for driving the Chemclaw agent locally — the testing front door.

Why this exists: the production ingress is Teams/Copilot Studio with native Entra-ID SSO
(architektur.md §7), so day-to-day there is no way to actually *talk* to the wired agent from a
checkout. This CLI is that seam for development and testing: it builds the same `build_agent`
the production host builds, opens the MCP capability subprocesses for the session, and runs a
turn-taking chat (or a single scripted question) against a live model.

Identity is the one thing that differs from production. Entra-ID auth is unimplemented (Phase 6),
so there is no token to resolve a real principal from. Rather than pretend, the CLI runs only in
explicit **admin mode** (`--admin`): it bypasses auth, advertises every skill (`allowed_skills=
None`), and stamps the GxP audit trail with the configured admin actor (`settings.cli_admin_actor`).
`resolve_identity` is the seam where Phase-6 Entra resolution will land as the non-admin branch;
today that branch fails loudly rather than silently running unauthenticated. Requiring the flag
keeps "no authentication" a conscious choice, not a default — the GxP posture, in a dev tool.

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
from chemclaw.config import settings
from chemclaw.logging import configure_logging

_EXIT_WORDS = {"exit", "quit", ":q"}


def resolve_identity(*, admin: bool, actor: str | None) -> tuple[str, set[str] | None]:
    """Resolve the caller's audit actor and the skills they may see — the Phase-6 identity seam.

    Returns `(actor, allowed_skills)` for `build_agent`. `allowed_skills=None` means "advertise
    every skill" (admin sees all). Entra-ID resolution (map the caller's app-roles/groups to an
    actor and a skill set) is the non-admin branch and is not built yet (Phase 6, plan 6.1/6.2),
    so without `--admin` this raises rather than running unauthenticated.

    Args:
        admin: Run in admin testing mode, bypassing the (unimplemented) Entra auth.
        actor: Override the audit actor label; defaults to `settings.cli_admin_actor`.
    """
    if not admin:
        raise SystemExit(
            "Entra-ID authentication is not implemented yet (Phase 6). Re-run with --admin to "
            "use the CLI unauthenticated for testing (bypasses auth; advertises all skills)."
        )
    return actor or settings.cli_admin_actor, None


def _build_cli_agent(args: argparse.Namespace) -> Any:
    """Build the agent for a CLI session from parsed args (identity + optional durable audit).

    The default chat client reads `ANTHROPIC_API_KEY` at construction and fails with a clear
    message if it is missing (D-037), so a credential problem surfaces here, before the prompt.
    """
    actor, allowed_skills = resolve_identity(admin=args.admin, actor=args.actor)
    sink: AuditSink | None = PostgresAuditSink() if args.audit_postgres else None
    return build_agent(actor=actor, audit_sink=sink, allowed_skills=allowed_skills)


async def converse(agent: Any, prompt: str) -> str:
    """Run one turn against the agent and return its text answer.

    The agent's session history provider accumulates the thread across calls, so reusing the same
    `agent` object over successive `converse` calls is a multi-turn conversation (no session
    plumbing needed here). MCP contexts must already be open (see `_run`).
    """
    response = await agent.run(prompt)
    return str(response.text)


async def _run(args: argparse.Namespace) -> None:
    """Build the agent, open its MCP subprocesses for the whole session, and dispatch.

    The MCP capability servers are spawned once for the session and torn down on exit (the
    `async with` over `agent.mcp_tools` the `build_agent` docstring prescribes), so a multi-turn
    REPL does not re-launch them per turn.
    """
    agent = _build_cli_agent(args)
    async with contextlib.AsyncExitStack() as stack:
        for tool in agent.mcp_tools:
            await stack.enter_async_context(tool)
        if args.message is not None:
            print((await converse(agent, args.message)).strip())
        else:
            await _repl(agent)


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
        "skills). Required until Phase-6 Entra auth lands.",
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
