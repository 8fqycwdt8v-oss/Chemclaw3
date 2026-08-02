"""Terminal CLI for driving the Chemclaw agent locally — the testing front door.

Why this exists: the production ingress is Teams/Copilot Studio with native Entra-ID SSO
(architektur.md §7), so day-to-day there is no way to actually *talk* to the wired agent from a
checkout. This CLI is that seam for development and testing: it builds the same `build_agent` the
production host builds, opens the MCP capability subprocesses for the session, and runs a
turn-taking chat (or a single scripted question) against a live model.

Identity is the one thing that differs from production. Entra-ID auth (F4, D-043) is a front-door
OIDC flow — it validates a browser-obtained token — and this is a terminal tool with no such token
to resolve a real principal from. Rather than pretend, the CLI runs only in explicit **admin mode**
(`--admin`): it bypasses auth and stamps the ambient identity (`chemclaw.core.identity_context`,
same seam
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

from chemclaw.agent.audit import AuditSink
from chemclaw.agent.audit_store import PostgresAuditSink
from chemclaw.agent.chemclaw_agent import build_agent, connector_tools
from chemclaw.connectors.registry import open_reachable
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.logging import configure_logging

_EXIT_WORDS = {"exit", "quit", ":q"}

# The two REPL lines that are operator commands rather than questions — the terminal's counterpart
# to `GET /sessions/{id}/plan` and `POST /sessions/{id}/plan/decision`.
_PLAN_COMMANDS = {"/plan", "/approve"}

# The session id every CLI run uses. A fixed name, not a fresh uuid: under `session_store=postgres`
# it makes a terminal session resumable across invocations, which is the CLI's actual use — and the
# CLI is single-user admin by construction (`resolve_identity`), so there is no one to collide with.
_CLI_SESSION_ID = "cli"


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
    # `--audit-postgres` now only *forces* the durable sink; omitting it no longer means log-only,
    # because `agents.audit.default_audit_sink` already gives a Postgres-configured deployment the
    # durable trail. The flag remains for the CLI's real case: an operator pointed at a database
    # for the calculation cache who wants the audit chain written too, without switching
    # `session_store` for a terminal session.
    sink: AuditSink | None = PostgresAuditSink() if args.audit_postgres else None
    return build_agent(actor=actor, audit_sink=sink)


async def converse(
    agent: Any, prompt: str, connectors: Sequence[Any] = (), session: Any = None
) -> str:
    """Run one turn against the agent on `session` and return its text answer.

    Reusing one `session` across successive `converse` calls is what makes the CLI a multi-turn
    conversation; the session's history provider accumulates the thread. The connectors must
    already be connected (see `_run`); they are passed per call because `Agent.run` is where
    run-scoped tools attach.

    **The session is not optional under `harness_enabled`** (D-152). The harness middleware stack
    that flag installs raises `ToolApprovalMiddleware requires an AgentSession` on a session-less
    `agent.run`, so the CLI — which used to rely on the agent's implicit thread — could not take a
    single turn under the configuration the shipped Helm chart sets. The front door always passed a
    session and never met this. It defaults to None only so the parameter stays additive for
    callers that build their own; `_run` always supplies one.
    """
    response = await agent.run(prompt, tools=list(connectors) or None, session=session)
    return str(response.text)


async def _run(args: argparse.Namespace) -> None:
    """Resolve identity, build the agent, open its MCP subprocesses, and dispatch.

    Identity is stamped ambient (`chemclaw.core.identity_context`) for the whole session — a CLI
    run is
    one actor throughout, unlike the multi-user front door, which stamps it per turn (F2/F4) —
    and reset on exit. The connectors are connected once for the whole CLI session and torn down on
    exit, which is sound here for the same reason it is not in the front door: a CLI run is
    single-user and single-threaded, so one connection cannot be shared across identities. An
    unreachable connector is skipped with a warning rather than aborting the session — the same
    degrade-loudly posture the front door takes.
    """
    actor, roles = resolve_identity(admin=args.admin, actor=args.actor)
    identity_token = set_current_identity(actor, roles)
    try:
        agent = _build_cli_agent(args, actor)
        # One session for the whole CLI run — a CLI run *is* one conversation. It is also required,
        # not merely tidy: the harness middleware refuses a session-less `agent.run` (D-152).
        session = agent.create_session(session_id=_CLI_SESSION_ID)
        # The default profile's connectors, matching `_build_cli_agent`, which builds the default
        # agent. Per-profile CLI selection waits for the front door to grow it (plan Stage D).
        connectors = connector_tools()
        async with contextlib.AsyncExitStack() as stack:
            # To stderr, with the answers on stdout: a piped `--message` run stays parseable while
            # a person at a terminal still learns the answer was assembled without those tools.
            # The docstring above has always claimed this warning; until REV-6 it was not emitted.
            for name in await open_reachable(stack, connectors):
                print(
                    f"warning: connector {name!r} is unreachable; its tools are unavailable",
                    file=sys.stderr,
                )
            if args.message is not None:
                print((await converse(agent, args.message, connectors, session)).strip())
            else:
                await _repl(agent, connectors, session)
    finally:
        reset_current_identity(identity_token)


async def _repl(agent: Any, connectors: Sequence[Any] = (), session: Any = None) -> None:
    """Read a question, print the answer, repeat — until EOF, Ctrl-C, or an exit word.

    Prompts/errors go to stderr so a redirected stdout carries only the answers.

    Two lines are commands rather than questions, `/plan` and `/approve`, and they exist because
    the plan gate is now enforced rather than merely recorded (D-167). Under `harness_enabled` with
    `plan_only` autonomy a state-changing tool needs a human approval for the plan it belongs to,
    and the front door's approval is an HTTP route — deliberately not an agent tool, so the model
    cannot approve its own candidate (D-005). A terminal with no way to answer would have left the
    CLI unable to write anything at all under the shipped Helm configuration, so it gets the same
    two operations the route pair offers, and for the same reason they are typed by the person
    rather than callable by the model.
    """
    print(
        "Chemclaw CLI — type a question, '/plan', '/approve', or 'exit' to quit.",
        file=sys.stderr,
    )
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
            if prompt.lower() in _PLAN_COMMANDS:
                print(await _plan_command(prompt, session), file=sys.stderr)
                continue
            print((await converse(agent, prompt, connectors, session)).strip())
        except Exception as exc:  # keep the session alive across a single failed turn
            print(f"error: {exc}", file=sys.stderr)


async def _plan_command(prompt: str, session: Any) -> str:
    """Run `/plan` or `/approve` against the session, returning the line to show the operator.

    `/approve` binds to the plan as it stands *now*, exactly as
    `POST /sessions/{id}/plan/decision` does — there is no hash to mistype here, but there is also
    no window in which a plan could change between being shown and being approved, because the
    person reading it and the person approving it are the same terminal.

    **Both commands ask `approvable_plan_hash`, not `current_plan_hash`, and that is the whole
    difference.** `/approve` used to guard on `todo_titles` and record against `current_plan_hash`,
    which are two different questions: `todo_titles` counts the `awaiting-job:` bookkeeping rows the
    launcher writes, and `current_plan_hash` falls back to `EMPTY_PLAN_HASH` for a session with no
    *work items*. So a session whose todo list held nothing but bookkeeping passed the guard and
    recorded an approval against the empty-plan constant — an identity every session in every
    deployment shares, which the gate then refuses. Harmless, and it told the person their plan was
    approved when nothing had been. The route learned to refuse that; this is the same refusal here,
    from the same function, so the two front doors cannot drift again.
    """
    from chemclaw.agent.harness_mode import approvable_plan_hash, grant_execute, session_mode
    from chemclaw.agent.harness_todo import todo_titles
    from chemclaw.agent.plan_approval_store import plan_approval_store

    if session is None:
        return "no session; plan commands need a chat session"
    plan_hash = await approvable_plan_hash(session)
    if prompt.lower() == "/plan":
        # Displayed, so it shows whatever the chemist is looking at — the checkboxes and the
        # bookkeeping rows included. Only the *decision* is restricted to a real plan.
        lines = await todo_titles(session) or ["(no plan yet)"]
        if plan_hash is None:
            return "\n".join([*lines, f"[no approvable plan, mode={session_mode(session)}]"])
        decision = await plan_approval_store().decision(session.session_id, plan_hash)
        # The store's verdict is already the effective one — a spent approval reports as not
        # approved — so this line says what the gate would do, not merely what was once recorded.
        verdict = "approved" if decision and decision[0] else "not approved"
        return "\n".join([*lines, f"[{plan_hash} — {verdict}, mode={session_mode(session)}]"])
    if plan_hash is None:
        return "there is no plan to approve yet; ask a question first"
    await plan_approval_store().record(
        session.session_id, plan_hash, settings.cli_admin_actor, True
    )
    grant_execute(session)
    return f"approved {plan_hash}; the session may now execute"


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
        help=(
            "Force the tool-audit trail to Postgres. Without it the trail is durable anyway "
            "wherever CHEMCLAW_SESSION_STORE=postgres, and log-only otherwise."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entrypoint (`chemclaw` console script / `python -m chemclaw.agent.cli`)."""
    configure_logging()
    asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    main()
