"""Terminal CLI for driving the Chemclaw agent locally — the testing front door.

Builds the same `build_langgraph_agent` graph the front door builds, opens the MCP connectors for
the session, and runs a turn-taking chat (or one scripted question) against a live model.

Identity is the one difference: there is no browser OIDC token here, so the CLI runs only in
explicit admin mode (`--admin`). That bypasses authentication and stamps the ambient identity
(`chemclaw.core.identity_context`) with `settings.cli_admin_actor` and `settings.cli_admin_roles`
(empty by default). It does not bypass authorization: the tool and expensive-trigger gates still
apply, so a full-access local seam requires populating `cli_admin_roles` deliberately.
`resolve_identity` fails loudly without `--admin`.

Run: `make chat`, `uv run chemclaw --admin`, or one-shot `uv run chemclaw --admin -m "…"`.
"""

import argparse
import asyncio
import contextlib
import sys
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from chemclaw.agent.audit import AuditSink
from chemclaw.agent.audit_store import PostgresAuditSink
from chemclaw.agent.checkpointer import process_checkpointer
from chemclaw.agent.chemclaw_agent import connector_specs
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.loop_cap import loop_capped
from chemclaw.agent.spend_cap import spend_capped
from chemclaw.agent.state import answer_text, turn_config, turn_input
from chemclaw.agent.turn_ambient import turn_caps
from chemclaw.agent.turn_usage import TurnUsage
from chemclaw.connectors.registry import open_connector_specs
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.llm_gateway import refuse_unconfigured_llm_gateway
from chemclaw.core.logging import configure_logging
from chemclaw.core.turn_text import reset_current_user_texts, set_current_user_texts

_EXIT_WORDS = {"exit", "quit", ":q"}

# Operator commands, not questions: the terminal's `GET /sessions/{id}/plan` and `POST
# /sessions/{id}/plan/decision`.
_PLAN_COMMANDS = {"/plan", "/approve"}

# The terminal's `GET /workflows/{name}` and `POST /workflows/{name}/approval`: a workflow must not
# approve itself, so a person types the approval. A workflow is keyed `(owner, name)` on the ambient
# actor, which in a dev deployment differs between the CLI and the front door, so the CLI needs its
# own approver.
_WORKFLOW_COMMANDS = {"/workflows", "/approve-workflow", "/forget-workflow"}

# The session id every CLI run uses: fixed, so it is a stable checkpointer `thread_id` and a
# terminal session resumes across invocations under `session_store=postgres`
# (`checkpointer.process_checkpointer`). Single-user admin, so nothing collides.
_CLI_SESSION_ID = "cli"

# Exit status of a one-shot `-m` run whose printed answer is incomplete; `1` means it never started.
_DEGRADED_EXIT = 2


class CliTurn(NamedTuple):
    """One CLI turn: the answer, and the sentence saying what is missing from it.

    `converse` holds the final state, from which a capped or empty turn is detectable; without a
    notice a loop-capped turn's interim sentence would print as the answer with exit 0. A log line
    about the loop is not a notice about the answer. `notice` is empty for a whole turn, so a clean
    run's output is unchanged.
    """

    answer: str
    notice: str = ""


def turn_notice(state: Mapping[str, Any], answer: str) -> str:
    """One line naming what is missing from `answer`, or `""` when nothing is.

    Ranked as `api/runner._settle_outcome` ranks them: both caps before the empty answer. Not shared
    with `durable.template_activities.AgentStepResult.notice`, which is prose for a later step, and
    importing `durable` would pull Temporal into the CLI.
    """
    if loop_capped(state):
        return "incomplete: the turn reached its model-call cap before it finished"
    if spend_capped(state):
        return "incomplete: the turn reached its token budget before it finished"
    if not answer:
        return "incomplete: the turn produced no answer"
    return ""


def resolve_identity(*, admin: bool, actor: str | None) -> tuple[str, frozenset[str]]:
    """Resolve the caller's audit actor and ambient roles — the CLI's identity seam.

    Returns `(actor, roles)`, stamped as the ambient identity for the whole session for audit,
    authorization and role-scoped skill visibility. There is no OIDC token to validate, so it runs
    only in admin mode. Roles come from `settings.cli_admin_roles` (empty by default), so `--admin`
    confers identity, not entitlement.

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

    `actor` is only the build-time audit fallback; audit, authz and skill scoping read the ambient
    identity `_run` stamps. No credential preflight: an empty `CHEMCLAW_LLM_API_KEY` is legitimate
    for keyless gateways, a gateway that wants one answers 401 on the first turn, and `_repl`
    survives it. A blank `CHEMCLAW_LLM_BASE_URL` is refused by `LlmSettings` and reported by `main`.
    Takes the connectors because a graph binds its tools at construction.
    """
    # `--audit-postgres` forces the durable sink; without it `default_audit_sink` still gives a
    # Postgres-configured deployment the durable trail. The flag serves an operator who wants the
    # audit chain written without switching `session_store`.
    sink: AuditSink | None = PostgresAuditSink() if args.audit_postgres else None
    return build_langgraph_agent(
        actor=actor, audit_sink=sink, connectors=list(connectors), checkpointer=saver
    )


async def converse(
    agent: Any,
    prompt: str,
    session_id: str = _CLI_SESSION_ID,
    earlier: Sequence[str] = (),
) -> CliTurn:
    """Run one turn on the graph under `session_id` and return its answer **and its notice**.

    `session_id` is the checkpointer's `thread_id`, so reusing it continues the conversation; the
    store is `checkpointer.process_checkpointer`'s choice. `earlier` is the operator's previous
    prompts in this REPL, oldest first.
    """
    # Stamp the chemist's own words for the turn, so `protocols` can check a `basis="stated"` quote
    # (`core.turn_text`); reset in a `finally`. The CLI writes no transcript, so `earlier` (this
    # process's prompts) stands in for the thread's user turns the front door reads; the bound is
    # `core.turn_text`'s on both surfaces.
    token = set_current_user_texts([*earlier, prompt])
    try:
        # The cap ambients, as every turn driver opens them: without the watch a `task` fan-out
        # would give every branch the whole iteration allowance. The repeat guard is among them, so
        # identical tool calls are refused here as at the front door. Nothing fills the token ledger
        # on this path, so the spend cap reads the channel alone.
        with turn_caps(TurnUsage(), closing=f"CLI session {session_id}"):
            result = await agent.ainvoke(
                turn_input(prompt),
                turn_config(session_id),
            )
    finally:
        reset_current_user_texts(token)
    # Read off the returned state, the only place either cap's flag lives: both channels are
    # untracked, so `get_state()` would answer `False`.
    answer = answer_text(result)
    return CliTurn(answer, turn_notice(result, answer))


async def _run(args: argparse.Namespace) -> int:
    """Resolve identity, build the agent, open its MCP subprocesses, and dispatch.

    Returns the exit status: `_DEGRADED_EXIT` when a one-shot `-m` run printed an answer marked
    incomplete, since the exit code is a piped run's only channel. Identity is stamped once for the
    whole session and reset on exit. Connectors are opened once for the session (safe here: one
    user, one thread); an unreachable one is skipped with a warning.
    """
    actor, roles = resolve_identity(admin=args.admin, actor=args.actor)
    identity_token = set_current_identity(actor, roles)
    try:
        async with contextlib.AsyncExitStack() as stack:
            # Opened before the graph is built, which binds its tools at construction. The default
            # profile's connectors, matching the default agent.
            connectors, unreachable = await open_connector_specs(stack, connector_specs())
            # To stderr, answers on stdout: a piped `--message` run stays parseable while a person
            # still learns tools were missing.
            for name in unreachable:
                print(
                    f"warning: connector {name!r} is unreachable; its tools are unavailable",
                    file=sys.stderr,
                )
            saver = await process_checkpointer()
            agent = _build_cli_agent(args, actor, connectors, saver)
            if args.message is not None:
                turn = await converse(agent, args.message)
                # Answer on stdout, notice on stderr, as for an unreachable connector above.
                print(turn.answer.strip())
                if turn.notice:
                    print(f"warning: {turn.notice}", file=sys.stderr)
                    return _DEGRADED_EXIT
            else:
                await _repl(agent, actor, saver)
    finally:
        reset_current_identity(identity_token)
    return 0


async def _repl(agent: Any, actor: str, saver: Any) -> None:
    """Read a question, print the answer, repeat — until EOF, Ctrl-C, or an exit word.

    Prompts and errors go to stderr so a redirected stdout carries only answers. `saver` is the
    checkpointer the graph was built on, so `/plan` reads the store the turns wrote to rather than
    the configured one. All arguments are required; `actor` is recorded as who approved, so it must
    never default.

    `/plan` and `/approve` mirror the front door's plan routes, typed by a person because the model
    must not approve its own plan. Note that `enforce_plan_approval` skips when no session id is
    set, and nothing here sets one, so the plan gate does not apply to this REPL. What governs a
    write typed here is who holds the terminal: `resolve_identity` makes this a single-user admin
    surface running with the process's credentials.
    """
    # Every operator command is named, since there is no `/help`.
    print(
        "Chemclaw CLI — type a question, or: /plan · /approve · /workflows · "
        "/approve-workflow <name> [<fingerprint>] · /forget-workflow <name> · exit",
        file=sys.stderr,
    )
    # What the operator typed, in order — the CLI's stand-in for the transcript the front door reads
    # back. Operator commands are excluded: they are instructions to the terminal, not words said to
    # the agent. Kept whole; the window is `core.turn_text`'s.
    said: list[str] = []
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
            if prompt.split(" ", 1)[0].lower() in _WORKFLOW_COMMANDS:
                print(await _workflow_command(prompt, actor), file=sys.stderr)
                continue
            turn = await converse(agent, prompt, earlier=said)
            # Recorded only once the turn answered, as the front door records nothing for an
            # unanswered turn.
            said.append(prompt)
            # Before the answer, so a reader who stops at the answer has read the notice. No exit
            # code: a REPL's status is the session's.
            if turn.notice:
                print(f"warning: {turn.notice}", file=sys.stderr)
            print(turn.answer.strip())
        except Exception as exc:  # keep the session alive across a single failed turn
            print(f"error: {exc}", file=sys.stderr)


async def _workflow_command(prompt: str, actor: str) -> str:
    """Run `/workflows` or `/approve-workflow <name>`, returning the line to show the operator.

    The terminal's half of an approval only a person may give: a composed workflow's durable `job`
    steps run only once somebody approves that exact document, and it is never an agent tool. The
    CLI needs its own approver because workflows are keyed on the ambient actor, which differs
    between surfaces in a dev deployment.

    `/approve-workflow` is read-then-approve: the first line prints the whole procedure (every
    step's call and arguments) and a fingerprint, and the second must type that fingerprint back.
    The agent acts in this terminal too and may re-compose the workflow between the two commands, so
    the approval binds to what was shown. `/workflows` lists names and counts for choosing one.
    `/forget-workflow <name>` frees a slot under `MAX_PER_OWNER`.

    Args:
        prompt: The typed line — `/workflows`, `/approve-workflow <name> [<fingerprint>]`, or
            `/forget-workflow <name>`.
        actor: This session's ambient actor, recorded as who approved; never defaulted.

    Returns:
        The lines to print on stderr.
    """
    from chemclaw.durable.template_job import template_fingerprint
    from chemclaw.templates.composed import (
        MAX_PER_OWNER,
        default_composed_store,
        job_steps,
        unapproved_jobs,
    )

    store = default_composed_store()
    # Split on whitespace so a stray third word is reported as a typo rather than read as part of
    # the fingerprint.
    command, *words = prompt.split()
    command = command.lower()
    name = words[0] if words else ""
    posted = words[1] if len(words) > 1 else ""
    if len(words) > 2:
        return f"usage: {command} <name> [<fingerprint>] — I do not know what {words[2]!r} means."
    if command == "/workflows":
        rows = await store.list_for(actor)
        if not rows:
            return "(no composed workflows)"
        # `list_for` fetches one past `MAX_PER_OWNER`; drop the spare row and say the list was
        # clamped.
        lines = []
        for row in rows[:MAX_PER_OWNER]:
            jobs = job_steps(row.document)
            withheld = unapproved_jobs(
                row.document, row.approved_fingerprint, template_fingerprint(row.document)
            )
            state = "needs approval" if withheld else "ready"
            detail = f", jobs: {jobs}" if jobs else ""
            lines.append(f"{row.name}  [{state}]  {len(row.document.steps)} step(s){detail}")
        if len(rows) > MAX_PER_OWNER:
            lines.append(
                f"(showing the {MAX_PER_OWNER} most recent; you are over the limit, so "
                "/forget-workflow one before composing another)"
            )
        return "\n".join(lines)
    if not name:
        return f"usage: {command} <name>  (see /workflows)"
    workflow = await store.get(actor, name)
    if workflow is None:
        available = [row.name for row in await store.list_for(actor)]
        return f"no composed workflow called {name!r}" + (
            f"; you have {available}" if available else ""
        )
    if command == "/forget-workflow":
        if not await store.forget(actor, name):
            return f"no composed workflow called {name!r}"
        return f"forgot {name!r}."
    fingerprint = template_fingerprint(workflow.document)
    jobs = job_steps(workflow.document)
    if not posted:
        # Every step is rendered, not just job steps: an approval covers the procedure, including
        # what feeds the jobs' arguments.
        return "\n".join(
            [
                f"{name!r} — {workflow.summary or '(no summary)'}",
                *_workflow_steps(workflow.document),
            ]
            + [
                (
                    f"approving releases job step(s) {jobs}."
                    if jobs
                    else "it has no job steps, so there is nothing to release."
                ),
                f"to approve exactly this: /approve-workflow {name} {fingerprint}",
            ]
        )
    if posted != fingerprint:
        # The workflow changed since it was shown, so the typed fingerprint names a different
        # procedure.
        return (
            f"{name!r} changed since it was shown — you typed {posted!r} and it is now "
            f"{fingerprint!r}. Read it again with `/approve-workflow {name}` before approving."
        )
    if not await store.approve(actor, name, fingerprint):
        return f"no composed workflow called {name!r}"
    return (
        f"approved {name!r} at {fingerprint}"
        + (f" — job step(s) {jobs} may now run" if jobs else " (it has no job steps to release)")
        + ". Composing it again needs approving again."
    )


def _workflow_steps(document: Any) -> list[str]:
    """One line per step, naming what it calls and with what.

    Through `composed.step_call`, as the HTTP approval screen (`api/routes/workflows._step_out`) is,
    so both surfaces show the same thing.

    Args:
        document: The resolved template being approved.

    Returns:
        The step lines, in declared order.
    """
    from chemclaw.templates.composed import step_call

    lines = []
    for step in document.steps:
        calls, arguments, prompt = step_call(step)
        detail = f"{calls}({arguments})" if calls else prompt
        lines.append(f"  {step.id}  [{step.kind}]  {detail}")
    return lines


async def _plan_command(prompt: str, actor: str, saver: Any) -> str:
    """Run `/plan` or `/approve` against the session, returning the line to show the operator.

    `saver` has no default: `session_plan` with `None` resolves the configured checkpointer, which
    under `session_store=memory` is not the store this session wrote to. `/approve` binds to the
    plan as it stands now, like `POST /sessions/{id}/plan/decision`. Both commands read the plan as
    the route does (`plan_state.session_plan`, hashed by `plan_gate.plan_identity`), so the two
    front doors agree on what is approved.
    """
    from chemclaw.agent.plan_approval_store import plan_approval_store
    from chemclaw.agent.plan_gate import plan_identity
    from chemclaw.agent.plan_scope import declared_scope
    from chemclaw.agent.plan_state import session_plan

    # `session_plan` answers `None` for an unreadable plan; for display that is the same as no plan,
    # and the gate tells them apart.
    steps = await session_plan(_CLI_SESSION_ID, saver=saver) or []
    plan = [str(step["content"]) for step in steps]
    scope = declared_scope(steps)
    # Hash the steps, not the text: the identity covers each step's declared tools, which is what
    # the gate asks about.
    plan_hash = plan_identity(steps)
    if prompt.lower() == "/plan":
        lines = plan or ["(no plan yet)"]
        if plan_hash is None:
            return "\n".join([*lines, "[no approvable plan]"])
        decision = await plan_approval_store().decision(_CLI_SESSION_ID, plan_hash)
        # The store's verdict is already effective (a spent approval reads as not approved).
        verdict = "approved" if decision and decision.approved else "not approved"
        # The declared tools are shown beside the steps: approving the plan approves them, and the
        # gate refuses any state-changing tool no step declared.
        declares = ", ".join(sorted(scope)) or "no state-changing tools"
        return "\n".join([*lines, f"[declares: {declares}]", f"[{plan_hash} — {verdict}]"])
    if plan_hash is None:
        return "there is no plan to approve yet; ask a question first"
    # `actor`, not `settings.cli_admin_actor`: the approval record must name the identity this
    # session runs under, as the audit rows do.
    await plan_approval_store().record(_CLI_SESSION_ID, plan_hash, actor, True, scope)
    # Recording is the whole grant.
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
        help="Ask one question and exit (scriptable), instead of the interactive REPL. "
        f"Exits {_DEGRADED_EXIT} when the answer it printed is incomplete — a cap was reached, or "
        "the turn produced nothing — with the reason on stderr.",
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


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint (`chemclaw` console script / `python -m chemclaw.cli.chat`).

    Startup failures become one sentence and an exit code rather than a traceback. Only the three
    families a startup can fail with are caught: misconfiguration (`ChemclawError`), a refused
    precondition (`RuntimeError`), and an unreachable dependency (`ConnectionError`); anything wider
    would hide programming errors. Failures inside a turn are handled in `_repl`.
    """
    configure_logging()
    try:
        # Inside the `try`, so an unconfigured gateway (`RuntimeError`) is reported like any startup
        # failure. The CLI builds the same graph `create_app` does, so the guard must reach it.
        refuse_unconfigured_llm_gateway()
        return asyncio.run(_run(_parse_args(argv)))
    except (ChemclawError, ConnectionError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
