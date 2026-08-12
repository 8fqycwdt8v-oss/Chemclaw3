"""Terminal CLI for driving the Chemclaw agent locally — the testing front door.

Why this exists: the production ingress is Teams/Copilot Studio with native Entra-ID SSO
(architektur.md §7), so day-to-day there is no way to actually *talk* to the wired agent from a
checkout. This CLI is that seam for development and testing: it builds the same `build_agent` the
production host builds, opens the MCP capability subprocesses for the session, and runs a
turn-taking chat (or a single scripted question) against a live model.

Identity is the one thing that differs from production. Entra-ID auth (F4, D-043) is a front-door
OIDC flow — it validates a browser-obtained token — and this is a terminal tool with no such token
to resolve a real principal from. Rather than pretend, the CLI runs only in explicit **admin mode**
(`--admin`): it bypasses **authentication** and stamps the ambient identity
(`chemclaw.core.identity_context`, the same seam the front door stamps per turn) with the configured
admin actor (`settings.cli_admin_actor`) and the roles in `settings.cli_admin_roles`, which is empty
by default.

**It does not bypass authorization.** Under `entra_required` the tool gate and the expensive-trigger
gate still apply, and an admin holding no privileged role is refused every expensive job and every
knowledge write — measured, 3 of 20 tools and 5 of 5 expensive actions refused on the shipped
config. That is the intended posture; a deployment wanting a full-access local seam populates
`cli_admin_roles` deliberately. The roles used to be derived from `skill_role_gates`, a *visibility*
map, so one overlapping role name silently made this terminal fully privileged.

`resolve_identity` is the seam where a non-admin branch could resolve identity
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
from chemclaw.agent.checkpointer import process_checkpointer
from chemclaw.agent.chemclaw_agent import connector_specs
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.state import turn_input
from chemclaw.connectors.registry import open_connector_specs
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.logging import configure_logging

_EXIT_WORDS = {"exit", "quit", ":q"}

# The two REPL lines that are operator commands rather than questions — the terminal's counterpart
# to `GET /sessions/{id}/plan` and `POST /sessions/{id}/plan/decision`.
_PLAN_COMMANDS = {"/plan", "/approve"}

# The session id every CLI run uses. A fixed name, not a fresh uuid: it is the checkpointer's
# `thread_id`, so under `session_store=postgres` it makes a terminal session resumable across
# invocations, which is the CLI's actual use — and the CLI is single-user admin by construction
# (`resolve_identity`), so there is no one to collide with. The resumability is
# `checkpointer.process_checkpointer`'s to deliver; until that function existed this comment
# described a property nothing provided.
_CLI_SESSION_ID = "cli"


def resolve_identity(*, admin: bool, actor: str | None) -> tuple[str, frozenset[str]]:
    """Resolve the caller's audit actor and ambient roles — the CLI's identity seam.

    Returns `(actor, roles)`, stamped as the ambient identity for the whole CLI session so audit
    attribution, the authorization gate, and role-scoped skill visibility all see it (F4). This CLI
    has no browser OIDC token to validate, so it runs only in admin mode.

    The roles come from `settings.cli_admin_roles` — empty by default, so `--admin` confers identity
    and no entitlement. They used to be the union of `settings.skill_role_gates`'s values, which
    coupled skill *visibility* to tool *authorization* through nothing but a shared role name.

    Args:
        admin: Run in admin testing mode, bypassing Entra *authentication* (this CLI has no token
            to check). Authorization still applies.
        actor: Override the audit actor label; defaults to `settings.cli_admin_actor`.
    """
    if not admin:
        raise SystemExit(
            "This CLI has no Entra-ID token to authenticate with (it is a terminal tool, not "
            "the front-door OIDC flow). Re-run with --admin to use the CLI unauthenticated for "
            "testing (bypasses authentication, not authorization: with no CHEMCLAW_CLI_ADMIN_ROLES "
            "set, expensive jobs and knowledge writes are still refused)."
        )
    return actor or settings.cli_admin_actor, frozenset(settings.cli_admin_roles)


def _build_cli_agent(
    args: argparse.Namespace, actor: str, connectors: Sequence[Any], saver: Any
) -> Any:
    """Compile the graph for a CLI session from parsed args, the actor, and the open connectors.

    `actor` is only the build-time audit fallback (used if a code path runs outside the ambient
    identity `_run` stamps for the session, e.g. a background task); the ambient identity is what
    audit/authz/skill-scoping actually read at call time. The chat model reads its credential at
    construction and fails with a clear message if it is missing (D-037), so a credential problem
    surfaces here, before the prompt.

    **Takes the connectors, because a graph binds its tools at construction.** MAF appended them
    per `agent.run`, so the agent could be built before they were open; a compiled graph cannot.
    That is why `_run` now opens the connectors first and builds second — the ordering is the
    engine's, and it applies here exactly as it does to a front-door turn.
    """
    # `--audit-postgres` now only *forces* the durable sink; omitting it no longer means log-only,
    # because `agents.audit.default_audit_sink` already gives a Postgres-configured deployment the
    # durable trail. The flag remains for the CLI's real case: an operator pointed at a database
    # for the calculation cache who wants the audit chain written too, without switching
    # `session_store` for a terminal session.
    sink: AuditSink | None = PostgresAuditSink() if args.audit_postgres else None
    return build_langgraph_agent(
        actor=actor, audit_sink=sink, connectors=list(connectors), checkpointer=saver
    )


async def converse(agent: Any, prompt: str, session_id: str = _CLI_SESSION_ID) -> str:
    """Run one turn on the graph under `session_id` and return its text answer.

    Reusing one `session_id` across successive calls is what makes the CLI a multi-turn
    conversation: it is the checkpointer's `thread_id`, so each turn continues the thread the last
    one left. Which store that is, and how long it lasts, is `checkpointer.process_checkpointer`'s
    decision — and until that function existed this sentence was false, because the graph was built
    with no checkpointer at all and every turn started empty.

    **The `session=` parameter is gone, and so is the reason it was mandatory.** Under MAF the
    harness middleware raised "ToolApprovalMiddleware requires an AgentSession" on a session-less
    `agent.run`, so the CLI could not take a single turn under the configuration the shipped Helm
    chart sets (D-152). A thread id is a string in a config dict; there is nothing to be absent.
    """
    result = await agent.ainvoke(
        turn_input(prompt),
        {"configurable": {"thread_id": session_id}},
    )
    return _answer_text(result)


def _answer_text(result: Any) -> str:
    """The final assistant text out of a completed graph turn."""
    messages = result.get("messages") or []
    if not messages:
        return ""
    content = messages[-1].content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content
        )
    return str(content)


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
        async with contextlib.AsyncExitStack() as stack:
            # Opened *before* the graph is built, because the graph binds its tools at
            # construction — see `_build_cli_agent`. The default profile's connectors, matching the
            # default agent; per-profile CLI selection waits for the front door to grow it
            # (plan Stage D).
            connectors, unreachable = await open_connector_specs(stack, connector_specs())
            # To stderr, with the answers on stdout: a piped `--message` run stays parseable while
            # a person at a terminal still learns the answer was assembled without those tools.
            # The docstring above has always claimed this warning; until REV-6 it was not emitted.
            for name in unreachable:
                print(
                    f"warning: connector {name!r} is unreachable; its tools are unavailable",
                    file=sys.stderr,
                )
            saver = await process_checkpointer()
            agent = _build_cli_agent(args, actor, connectors, saver)
            if args.message is not None:
                print((await converse(agent, args.message)).strip())
            else:
                await _repl(agent, actor, saver)
    finally:
        reset_current_identity(identity_token)


async def _repl(agent: Any, actor: str, saver: Any) -> None:
    """Read a question, print the answer, repeat — until EOF, Ctrl-C, or an exit word.

    Prompts/errors go to stderr so a redirected stdout carries only the answers.

    `saver` is the checkpointer the graph was built on, threaded through so `/plan` reads the store
    the turns actually wrote to. Passing it rather than letting `session_todos` resolve one is the
    whole fix: resolving gives the *configured* checkpointer, which under `session_store=memory` is
    not the one the graph holds and under either setting was not the one an unwired graph wrote to.

    All three arguments are required. `actor` in particular must never default: it flows into
    `plan_approval_store().record(...)`, whose entire purpose is that the GxP record names the
    identity that approved, and a default that would write an anonymous approval is not a safe
    fallback but one that must never be taken.

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
                print(await _plan_command(prompt, actor, saver), file=sys.stderr)
                continue
            print((await converse(agent, prompt)).strip())
        except Exception as exc:  # keep the session alive across a single failed turn
            print(f"error: {exc}", file=sys.stderr)


async def _plan_command(prompt: str, actor: str, saver: Any) -> str:
    """Run `/plan` or `/approve` against the session, returning the line to show the operator.

    **`saver` has no default**, and that is the fix rather than a style choice: `session_todos`
    resolves the *configured* checkpointer when handed `None`, which under `session_store=memory`
    is not the store this session's turns wrote to. A default here would leave the defect reachable
    by omission — `/plan` answering "(no plan yet)" for a session that has one, and `/approve`
    recording against the empty-plan constant every deployment shares.

    `/approve` binds to the plan as it stands *now*, exactly as
    `POST /sessions/{id}/plan/decision` does — there is no hash to mistype here, but there is also
    no window in which a plan could change between being shown and being approved, because the
    person reading it and the person approving it are the same terminal.

    **Both commands read the plan the same way the route does** — `plan_state.session_todos` off
    the checkpointer, hashed by `plan_gate.plan_identity` — which is what keeps the two front doors
    from drifting. They used to ask two different questions: `/approve` guarded on `todo_titles`
    and recorded against `current_plan_hash`, and those disagreed about the `awaiting-job:`
    bookkeeping rows, so a session whose list held nothing else passed the guard and recorded an
    approval against the empty-plan constant — an identity every deployment shares, which the gate
    then refuses. It told the person their plan was approved when nothing had been.
    """
    from chemclaw.agent.plan_approval_store import plan_approval_store
    from chemclaw.agent.plan_gate import plan_identity
    from chemclaw.agent.plan_state import session_todos

    # `or []`: `session_todos` answers `None` when the plan could not be *read* at all, which for
    # this command is the same screen as a session that has proposed nothing — the gate is what
    # must tell the two apart, not the display.
    plan = await session_todos(_CLI_SESSION_ID, saver=saver) or []
    plan_hash = plan_identity(plan)
    if prompt.lower() == "/plan":
        lines = plan or ["(no plan yet)"]
        if plan_hash is None:
            return "\n".join([*lines, "[no approvable plan]"])
        decision = await plan_approval_store().decision(_CLI_SESSION_ID, plan_hash)
        # The store's verdict is already the effective one — a spent approval reports as not
        # approved — so this line says what the gate would do, not merely what was once recorded.
        verdict = "approved" if decision and decision[0] else "not approved"
        return "\n".join([*lines, f"[{plan_hash} — {verdict}]"])
    if plan_hash is None:
        return "there is no plan to approve yet; ask a question first"
    # `actor`, not `settings.cli_admin_actor`. The session runs under whatever `--actor` resolved
    # to, and every other identity consumer in this module reads that — so hardcoding the default
    # made the durable approval record, which is the artifact of the "AI proposes, human signs off"
    # line, name an identity that took no action and disagree with the audit rows for its own
    # session.
    await plan_approval_store().record(_CLI_SESSION_ID, plan_hash, actor, True)
    # Recording is the whole grant. It used to also call `grant_execute` to flip MAF's session
    # mode — a second piece of state saying the same thing on a different lifetime, which is what
    # let a displayed mode outlive the approval it came from.
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
        help="Run unauthenticated as the admin actor. Bypasses Entra *authentication* only — "
        "authorization still applies, and the roles come from CHEMCLAW_CLI_ADMIN_ROLES, empty by "
        "default, so this confers identity and no entitlement. Required — this terminal tool has "
        "no front-door OIDC token to check.",
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
