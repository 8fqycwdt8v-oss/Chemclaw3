"""Count recurring tool-call trajectories over the stored sessions.

The instrument `D-2026-08-27-count-the-trajectories-before-building-the-distiller` defines as the
trigger for building a trajectory→skill distiller. Two arms:

* **Procedure**: per-turn tool-call sequences (consecutive duplicates collapsed), classes of
  length ≥ 2 recurring across sessions, and those a later session repeated after an earlier one
  completed them.
* **Failure**: tools that errored across sessions, and those a later session hit again after an
  earlier session recovered — a recurring failure produces divergent sequences, which the first
  arm cannot see.

Reads `session_messages` through `session_store.message_from_row`. An empty store prints zeros
rather than refusing; a deployment pruning the table by retention measures its retention window.
"""

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from chemclaw.agent.distiller import MIN_INDEPENDENT_SESSIONS


@dataclass(frozen=True)
class Turn:
    """One turn's normalized trajectory: where it happened, when, and which tools in what order."""

    session_id: str
    at: datetime | None
    tools: tuple[str, ...]


def normalized_tools(messages: list[BaseMessage]) -> list[tuple[str, ...]]:
    """Each turn's tool-name sequence, consecutive duplicates collapsed (a retry is not a step).

    A turn is what lies between one `HumanMessage` and the next; the sequence is read off every
    `AIMessage.tool_calls` inside it, in order.
    """
    turns: list[tuple[str, ...]] = []
    current: list[str] = []
    started = False
    for message in messages:
        if isinstance(message, HumanMessage):
            if started:
                turns.append(tuple(current))
            current = []
            started = True
        elif isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                name = str(call.get("name", ""))
                if name and (not current or current[-1] != name):
                    current.append(name)
    if started:
        turns.append(tuple(current))
    return turns


@dataclass(frozen=True)
class SessionFailures:
    """One session's failure surface: which tools errored in it, and which of those later worked.

    Session-scoped, not turn-scoped: a recovery two turns after the failure is still a recovery.
    """

    session_id: str
    at: datetime | None
    failed: frozenset[str]
    recovered: frozenset[str]


def failed_and_recovered(messages: list[BaseMessage]) -> tuple[frozenset[str], frozenset[str]]:
    """Which tools errored in this session, and which of those later returned a result that did not.

    A `ToolMessage` carries `status` and `tool_call_id`; the tool's name is on the issuing
    `AIMessage.tool_calls` entry, so the two are joined. "Later returned a non-error result" is a
    proxy for recovery, not proof.

    A result with no `status` counts as success, matching `api/graph_stream.py`'s
    `getattr(message, "status", "success")`.
    """
    names: dict[str, str] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                call_id, name = str(call.get("id", "")), str(call.get("name", ""))
                if call_id and name:
                    names[call_id] = name
    failed: set[str] = set()
    recovered: set[str] = set()
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        name = names.get(str(getattr(message, "tool_call_id", "")), "")
        if not name:
            continue
        if getattr(message, "status", "success") == "error":
            failed.add(name)
        elif name in failed:
            recovered.add(name)
    return frozenset(failed), frozenset(recovered)


def _failure_census(sessions: list[SessionFailures]) -> dict[str, Any]:
    """The failure arm: failure classes recurring across sessions, and repeats after a fix.

    A failure class is one tool name that errored; it recurs when it errored in ≥ 2 sessions. It was
    repeated after recovery when a session that failed and recovered on it precedes one that failed
    again. A missing timestamp counts as not-repeated, so unknown order never inflates the trigger.
    """
    by_tool: dict[str, list[SessionFailures]] = defaultdict(list)
    for session in sessions:
        for name in session.failed:
            by_tool[name].append(session)
    classes: list[dict[str, Any]] = []
    for name, failing in sorted(by_tool.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len({s.session_id for s in failing}) < 2:
            continue
        fixed = [s.at for s in sessions if name in s.recovered and s.at is not None]
        again = [s.at for s in failing if s.at is not None]
        repeated = bool(fixed) and bool(again) and min(fixed) < max(again)
        classes.append(
            {
                "tool": name,
                "sessions": len({s.session_id for s in failing}),
                "recovered_somewhere": any(name in s.recovered for s in sessions),
                "repeated_after_recovery": repeated,
            }
        )
    spread = len({s.session_id for row in classes for s in by_tool[str(row["tool"])]})
    repeats = sum(1 for row in classes if row["repeated_after_recovery"])
    return {
        "sessions_with_a_failure": len({s.session_id for s in sessions if s.failed}),
        "failure_classes": classes,
        "trigger": {
            "failure_classes": len(classes),
            "sessions_with_failure_recurrence": spread,
            "repeated_after_recovery_classes": repeats,
            # The second arm's stated greenlight, evaluated here for the same reason the first
            # arm's is: the verdict belongs in the output, not in whoever reads it.
            "failure_greenlit": len(classes) >= 3 and spread >= 3 and repeats >= 1,
        },
    }


def census(turns: list[Turn], failures: list[SessionFailures] | None = None) -> dict[str, Any]:
    """Reduce the corpus to both arms' numbers: recurring procedure, and recurring failure.

    A class recurs when the identical sequence of length ≥ 2 appears in ≥ 2 sessions; it would have
    helped when a later session's first occurrence follows an earlier one's. A missing timestamp
    counts as not-helped, so unknown order never inflates the trigger.

    `generator_greenlit` answers the procedure arm alone; `any_greenlit` is the disjunction a reader
    should act on. `failures` defaults to none.
    """
    by_class: dict[tuple[str, ...], list[Turn]] = defaultdict(list)
    for turn in turns:
        if len(turn.tools) >= 2:
            by_class[turn.tools].append(turn)
    recurring: list[dict[str, Any]] = []
    for tools, occurrences in sorted(by_class.items(), key=lambda kv: -len(kv[1])):
        sessions = {t.session_id for t in occurrences}
        if len(sessions) < MIN_INDEPENDENT_SESSIONS:
            continue
        first_by_session: dict[str, datetime | None] = {}
        for turn in occurrences:
            seen = first_by_session.get(turn.session_id)
            if turn.session_id not in first_by_session or (
                turn.at is not None and (seen is None or turn.at < seen)
            ):
                first_by_session[turn.session_id] = turn.at
        stamps = sorted(at for at in first_by_session.values() if at is not None)
        helped = len(stamps) >= 2 and stamps[0] < stamps[-1]
        recurring.append(
            {
                "tools": list(tools),
                "occurrences": len(occurrences),
                "sessions": len(sessions),
                "would_have_helped": helped,
                "multi_tool": len(tools) >= 3,
            }
        )
    helped_multi = [r for r in recurring if r["would_have_helped"] and r["multi_tool"]]
    failure_arm = _failure_census(failures or [])
    procedure_greenlit = (
        len(recurring) >= 5
        and len({t.session_id for r in recurring for t in by_class[tuple(r["tools"])]}) >= 3
        and len(helped_multi) >= 1
    )
    return {
        "turns": len(turns),
        "multi_tool_turns": sum(1 for t in turns if len(t.tools) >= 2),
        "sessions": len({t.session_id for t in turns}),
        "recurring_classes": recurring,
        "trigger": {
            "recurring_classes": len(recurring),
            "sessions_with_recurrence": len(
                {t.session_id for r in recurring for t in by_class[tuple(r["tools"])]}
            ),
            "helped_multi_tool_classes": len(helped_multi),
            # The ADR's stated greenlight, evaluated so the answer is in the output rather than in
            # whoever reads it.
            "generator_greenlit": procedure_greenlit,
            **failure_arm["trigger"],
            "any_greenlit": procedure_greenlit or failure_arm["trigger"]["failure_greenlit"],
        },
        "sessions_with_a_failure": failure_arm["sessions_with_a_failure"],
        "failure_classes": failure_arm["failure_classes"],
    }


async def _stored() -> tuple[list[Turn], list[SessionFailures]]:
    """Every stored session's turns and failure surface, decoded through the one sanctioned reader.

    One query for both arms, so they cover the same sessions.
    """
    from chemclaw.agent.session_store import message_from_row
    from chemclaw.core import db
    from chemclaw.core.config import settings

    turns: list[Turn] = []
    failures: list[SessionFailures] = []
    async with db.connection(settings.postgres_dsn) as conn:
        cursor = await conn.execute(
            "SELECT session_id, message, message_shape, created_at "
            "FROM session_messages ORDER BY session_id, id"
        )
        rows = await cursor.fetchall()
    by_session: dict[str, list[tuple[BaseMessage, datetime | None]]] = defaultdict(list)
    for session_id, payload, shape, created_at in rows:
        by_session[str(session_id)].append((message_from_row(payload, shape), created_at))
    for session_id, dated in by_session.items():
        messages = [m for m, _ in dated]
        stamps = [at for _, at in dated]
        # The turn's timestamp is the session's first row's — coarse, and enough: would-have-helped
        # orders *sessions*, not turns within one.
        at = next((s for s in stamps if s is not None), None)
        for tools in normalized_tools(messages):
            turns.append(Turn(session_id=session_id, at=at, tools=tools))
        failed, recovered = failed_and_recovered(messages)
        failures.append(
            SessionFailures(session_id=session_id, at=at, failed=failed, recovered=recovered)
        )
    return turns, failures


def main() -> None:
    """Run the census over the configured database and print the ADR's numbers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the machine-readable form")
    args = parser.parse_args()
    report = census(*asyncio.run(_stored()))
    if args.json:
        print(json.dumps(report, indent=2))
        return
    trigger = report["trigger"]
    print(f"sessions: {report['sessions']}   turns: {report['turns']}")
    print(f"multi-tool turns (>=2 tools): {report['multi_tool_turns']}")
    print(f"recurring classes (>=2 sessions): {trigger['recurring_classes']}")
    print(f"  of which would-have-helped and >=3 tools: {trigger['helped_multi_tool_classes']}")
    for row in report["recurring_classes"][:20]:
        helped = "would have helped" if row["would_have_helped"] else "same-day only"
        print(
            f"  {' -> '.join(row['tools'])}  x{row['occurrences']} "
            f"in {row['sessions']} session(s), {helped}"
        )
    print(f"sessions with a failed tool call: {report['sessions_with_a_failure']}")
    print(f"recurring failure classes (>=2 sessions): {trigger['failure_classes']}")
    print(
        "  of which repeated after an earlier session recovered: "
        f"{trigger['repeated_after_recovery_classes']}"
    )
    for row in report["failure_classes"][:20]:
        note = (
            "repeated after recovery"
            if row["repeated_after_recovery"]
            else ("recovered somewhere" if row["recovered_somewhere"] else "never recovered")
        )
        print(f"  {row['tool']}  failed in {row['sessions']} session(s), {note}")
    procedure = "GREENLIT" if trigger["generator_greenlit"] else "not greenlit"
    failure = "GREENLIT" if trigger["failure_greenlit"] else "not greenlit"
    print(
        f"procedure arm (D-2026-08-27-count-the-trajectories...): {procedure} "
        "(>=5 classes, >=3 sessions, >=1 helped multi-tool)"
    )
    print(
        f"failure arm (D-2026-09-05-a-census-that-counts-only-success...): {failure} "
        "(>=3 classes, >=3 sessions, >=1 repeated after recovery)"
    )
    print(f"distiller: {'GREENLIT' if trigger['any_greenlit'] else 'not greenlit'} (either arm)")
    print(
        "note: this measures session_messages as retained — a deployment pruning by age is "
        "measuring its retention window",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
