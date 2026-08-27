"""Count recurring tool-call trajectories over the stored sessions.

The instrument `D-2026-08-27-count-the-trajectories-before-building-the-distiller` defines: before
anyone builds a trajectory→skill distiller, the sessions on disk have to show enough recurrence to
distill — and "enough" is that ADR's stated trigger, not a feeling. This command produces the
number: per-turn tool-call sequences (consecutive duplicates collapsed), the classes of length ≥ 2
that recur across sessions, and the subset where a later session repeated a sequence an earlier
one had already completed — the case where a distilled skill could actually have changed a later
answer.

Reads the durable read-model (`session_messages`) through `session_store.message_from_row`, the
one function allowed to decide which serialization a row holds. An empty store prints zeros rather
than refusing, so "the corpus does not exist" stays a claim anyone can re-produce with one command
— and a deployment that prunes this table by retention is measuring its retention window, which the
output says.
"""

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage


@dataclass(frozen=True)
class Turn:
    """One turn's normalized trajectory: where it happened, when, and which tools in what order."""

    session_id: str
    at: datetime | None
    tools: tuple[str, ...]


def normalized_tools(messages: list[BaseMessage]) -> list[tuple[str, ...]]:
    """Each turn's tool-name sequence, consecutive duplicates collapsed (a retry is not a step).

    A turn is what lies between one `HumanMessage` and the next; the sequence is read off every
    `AIMessage.tool_calls` inside it, in order. Split out from the census so the definition the
    ADR states is one function a test can hold, independent of any database.
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


def census(turns: list[Turn]) -> dict[str, Any]:
    """Reduce the turns to the ADR's numbers: recurrence across sessions, and would-have-helped.

    A class recurs when the identical sequence of length ≥ 2 appears in ≥ 2 distinct sessions; it
    would have helped when some later session's first occurrence follows an earlier session's —
    ordering decided by the rows' own timestamps, and left undecided (counted not-helped) when a
    timestamp is missing, because an unknown order must not inflate the number that greenlights a
    build.
    """
    by_class: dict[tuple[str, ...], list[Turn]] = defaultdict(list)
    for turn in turns:
        if len(turn.tools) >= 2:
            by_class[turn.tools].append(turn)
    recurring: list[dict[str, Any]] = []
    for tools, occurrences in sorted(by_class.items(), key=lambda kv: -len(kv[1])):
        sessions = {t.session_id for t in occurrences}
        if len(sessions) < 2:
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
            "generator_greenlit": len(recurring) >= 5
            and len({t.session_id for r in recurring for t in by_class[tuple(r["tools"])]}) >= 3
            and len(helped_multi) >= 1,
        },
    }


async def _stored_turns() -> list[Turn]:
    """Every stored session's turns, decoded through the one sanctioned row reader."""
    from chemclaw.agent.session_store import message_from_row
    from chemclaw.core import db
    from chemclaw.core.config import settings

    turns: list[Turn] = []
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
    return turns


def main() -> None:
    """Run the census over the configured database and print the ADR's numbers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the machine-readable form")
    args = parser.parse_args()
    report = census(asyncio.run(_stored_turns()))
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
    verdict = "GREENLIT" if trigger["generator_greenlit"] else "not greenlit"
    print(
        f"distiller trigger (D-2026-08-27-count-the-trajectories...): {verdict} "
        "(>=5 classes, >=3 sessions, >=1 helped multi-tool)"
    )
    print(
        "note: this measures session_messages as retained — a deployment pruning by age is "
        "measuring its retention window",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
