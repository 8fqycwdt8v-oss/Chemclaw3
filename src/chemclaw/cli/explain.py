"""Reconstruct why a session's tool calls happened — the join made usable.

Joins `audit_events.session_id` with `session_messages.correlation_id`
(D-2026-07-31-the-audit-chain-is-versioned): given a session, print the conversation and, under each
turn, the tools that ran because of it, the plan step each call served (`audit_events.plan_step`,
including refused calls), a durable job's stated rationale where one exists, and how the turn ended
(`turn_costs`), so a capped or failed turn does not read as a clean one.

Read-only: five `SELECT`s, no writes. Deliberately not an agent tool: the audit trail is evidence
about the agent, to be examined rather than summarized by it.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import NamedTuple

from chemclaw.agent.session_store import is_degraded_render, message_from_row
from chemclaw.api.schemas import message_role, message_text
from chemclaw.core.config import settings
from chemclaw.core.db import connection

# One turn's words, in order. `created_at` orders rows written before `correlation_id` existed (they
# carry '' and form one "unattributed" group).
_MESSAGES = """
    SELECT correlation_id, message, message_shape, created_at
    FROM session_messages
    WHERE session_id = %s
    ORDER BY id ASC
"""

# `plan_step`, not `purpose`: `purpose` is never written (nothing can author it honestly; see
# `agent/audit.AuditEvent.purpose`), while `plan_step` exactly answers which plan step was in
# flight. Ordered by `ts` before `id`, because `id` is assigned when the batching sink flushes; `id`
# breaks ties within one clock tick.
_AUDIT = """
    SELECT correlation_id, tool, outcome, detail, latency_ms, actor, plan_step, agent
    FROM audit_events
    WHERE session_id = %s
    ORDER BY ts ASC, id ASC
"""

# A durable job states its reason outright, so it is printed verbatim.
_JOBS = """
    SELECT correlation_id, connector, job, rationale, summary
    FROM job_records
    WHERE session_id = %s
    ORDER BY completed_at ASC
"""

# How each turn ended, keyed on the same correlation id as every group above. Only the columns about
# the answer are read — `outcome`, `completed`, `compacted`, `context_unreducible`,
# `answer_confidence`, `review_required` — not spend. Ordered so the last row for an id wins; a
# template's agent steps book under suffixed ids, so each stands on its own.
_COSTS = """
    SELECT correlation_id, outcome, completed, compacted, context_unreducible,
           answer_confidence, review_required, error_code
    FROM turn_costs
    WHERE session_id = %s
    ORDER BY recorded_at ASC
"""

# Whether the session was ever created here: `session_owners` gets one row per session at creation,
# so this tells a mistyped id from a session that ran and left nothing. The owner itself is not
# printed.
_OWNED = """
    SELECT 1
    FROM session_owners
    WHERE session_id = %s
"""


class ToolCall(NamedTuple):
    """One audited tool invocation as this report shows it."""

    tool: str
    outcome: str
    detail: str
    latency_ms: float
    actor: str
    plan_step: str
    #: The `AgentProfile` name of the graph that made the call, empty for the agent the chemist was
    #: talking to (`agent/audit.AuditEvent.agent`); rendered only when non-empty. Defaulted for
    #: positional construction in tests; the fetch always supplies it.
    agent: str = ""


class Job(NamedTuple):
    """One finished durable job, with the reason its launcher was required to state."""

    connector: str
    job: str
    rationale: str
    summary: str


class TurnEnd(NamedTuple):
    """How one turn ended, from `turn_costs`.

    Every field is a stored fact, not an inference. All defaulted, for positional construction in
    tests and because the columns arrived across migrations; `outcome='unknown'` means the row
    predates the column.
    """

    outcome: str = "unknown"
    completed: bool = True
    compacted: bool = False
    context_unreducible: bool = False
    answer_confidence: float | None = None
    review_required: bool = False
    error_code: str = ""

    def line(self) -> str:
        """This turn's ending as one printable line.

        `answered` still prints, so the absence of a line means the deployment does not record
        endings, not that the turn ended cleanly.
        """
        notes = []
        if not self.completed:
            notes.append("no answer delivered")
        if self.error_code:
            notes.append(f"error {self.error_code}")
        if self.compacted:
            notes.append("context compacted")
        if self.context_unreducible:
            notes.append("context over budget and unreducible")
        if self.answer_confidence is not None:
            notes.append(f"confidence {self.answer_confidence:.2f}")
        if self.review_required:
            notes.append("flagged for review")
        detail = f" ({'; '.join(notes)})" if notes else ""
        return f"   ended: {self.outcome}{detail}"


def _speaker(message: object, shape: str | None = None) -> tuple[str, str]:
    """The `(role, text)` of a stored message, tolerating shapes this tool did not write.

    Read through `session_store.message_from_row`, which handles both stored serializations. A
    reconstruction must not fail on an unparseable message, so an unreadable payload renders as its
    repr under an `unknown` role. A row the store could only recover (`is_degraded_render`) also
    gets `unknown`, since its speaker was guessed; if its render is empty, the repr is shown rather
    than dropping a row that is on disk.
    """
    if not isinstance(message, dict):
        return "unknown", str(message)
    try:
        restored = message_from_row(message, shape)
    except Exception:
        return "unknown", str(message)
    if is_degraded_render(restored):
        return "unknown", message_text(restored).strip() or str(message)
    # The transcript route's own projection, so roles match the browser. A readable row with no
    # prose (e.g. an image part) keeps its role and emptiness; only an unrenderable row is shown as
    # a repr.
    return message_role(restored), message_text(restored).strip()


def _why_nothing(known: bool) -> list[str]:
    """Why this reconstruction is empty, so the reader knows which of three situations they are in.

    Under `session_store="memory"` nothing is recorded, so it names the setting. Under `postgres`,
    an id with no `session_owners` row was never created here (a typo, another deployment or
    database); an id with one ran and left nothing (retention, an abandoned turn, or no turn taken).
    """
    if settings.session_store != "postgres":
        return [
            "  this deployment records nothing: CHEMCLAW_SESSION_STORE="
            f"{settings.session_store}, so `default_audit_sink()` is log-only and no transcript "
            "row is written either. An unknown id and a session that ran a hundred tools print "
            "exactly this. Set CHEMCLAW_SESSION_STORE=postgres to keep the record.",
        ]
    if known:
        return [
            "  the session exists (session_owners has its row) and nothing is recorded under it — "
            "not even a turn-cost row, which is written for a turn that was abandoned or errored: "
            "retention has pruned it, or it never took a turn.",
        ]
    return [
        "  no session with this id was ever created against this database — check the id, the "
        "deployment and CHEMCLAW_POSTGRES_DSN. This is not an empty session; it is an unknown one.",
    ]


def _is_database_refusal(exc: BaseException) -> bool:
    """Whether `exc` is the driver saying no, identified without importing the driver.

    `chemclaw.cli` may not import `psycopg` (`tests/test_third_party_layering.py`), so this checks
    the two attributes every `psycopg.Error` carries and nothing else here does: `sqlstate` and
    `diag`. Its proper home is `core/db`, whose `ConnectionError` mapping covers only
    `OperationalError`; widening that is a separate decision.
    """
    return hasattr(exc, "sqlstate") and hasattr(exc, "diag")


def _wrap(text: str, *, limit: int = 400) -> str:
    """One line, bounded — a turn's transcript can be long and this is an index, not an archive."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


async def explain(session_id: str, dsn: str | None = None) -> list[str]:
    """Return the reconstruction of `session_id` as printable lines, newest turn last."""
    target = dsn if dsn is not None else settings.postgres_dsn
    turns: dict[str, list[tuple[str, str]]] = {}
    order: list[str] = []
    calls: dict[str, list[ToolCall]] = {}
    jobs: dict[str, list[Job]] = {}
    ends: dict[str, TurnEnd] = {}

    async with connection(target) as conn:
        cursor = await conn.execute(_MESSAGES, (session_id,))
        for correlation_id, message, shape, _created in await cursor.fetchall():
            if correlation_id not in turns:
                turns[correlation_id] = []
                order.append(correlation_id)
            role, text = _speaker(message, shape)
            if text:
                turns[correlation_id].append((role, text))

        cursor = await conn.execute(_AUDIT, (session_id,))
        for (
            correlation_id,
            tool,
            outcome,
            detail,
            latency,
            actor,
            plan_step,
            agent,
        ) in await cursor.fetchall():
            calls.setdefault(correlation_id, []).append(
                ToolCall(tool, outcome, detail, latency, actor, plan_step, agent)
            )

        # `job_records` is keyed independently, so a job without a correlation id still belongs to
        # the session.
        cursor = await conn.execute(_JOBS, (session_id,))
        for correlation_id, connector, job, rationale, summary in await cursor.fetchall():
            jobs.setdefault(correlation_id, []).append(Job(connector, job, rationale, summary))

        cursor = await conn.execute(_COSTS, (session_id,))
        for correlation_id, *fields in await cursor.fetchall():
            # Last row wins for a correlation id (see the query's ordering).
            ends[correlation_id] = TurnEnd(*fields)

        cursor = await conn.execute(_OWNED, (session_id,))
        known = await cursor.fetchone() is not None

    return _render(session_id, order, turns, calls, jobs, ends, known=known)


def _render(
    session_id: str,
    order: list[str],
    turns: dict[str, list[tuple[str, str]]],
    calls: dict[str, list[ToolCall]],
    jobs: dict[str, list[Job]],
    ends: dict[str, TurnEnd] | None = None,
    *,
    known: bool = False,
) -> list[str]:
    """Format the gathered rows; separated from the fetch so it is testable without a database.

    Turns the audit trail or job records know about are shown even without a transcript row:
    retention prunes messages, and a failed or abandoned turn never writes one, so the trail
    routinely outlives the words. One insertion-ordered pass over every source, so each turn is
    shown once.
    """
    endings = ends or {}
    # `endings` is a fourth source of turns: a cancelled turn writes a cost row and nothing else,
    # and a stored fact must not be rendered as a guess.
    shown = list(dict.fromkeys([*order, *calls, *jobs, *endings]))
    lines = [f"session {session_id}", ""]
    if not shown:
        lines.append("  no messages, tool calls, jobs or turn records for this session")
        lines.extend(_why_nothing(known))
        return lines
    for correlation_id in shown:
        label = correlation_id or "(unattributed — written before the correlation id was recorded)"
        lines.append(f"── turn {label}")
        said = turns.get(correlation_id, [])
        if not said:
            # The ledger is consulted before guessing: an errored or abandoned turn is the commonest
            # reason for a missing transcript row.
            ending = endings.get(correlation_id)
            if ending is not None and ending.outcome != "answered":
                lines.append(
                    f"   transcript: absent (the turn ended {ending.outcome} "
                    "before one was written)"
                )
            else:
                lines.append("   transcript: absent (compacted, pruned, or rolled back)")
        for role, text in said:
            # A stored tool result is not a speaker; label it as a result so it is not read as
            # something said. The audit row records that a call happened, never what it returned.
            label = "tool result" if role == "tool" else role
            lines.append(f"   {label}: {_wrap(text)}")
        for job in jobs.get(correlation_id, []):
            lines.append(f"   job {job.connector}:{job.job} — because: {_wrap(job.rationale)}")
            lines.append(f"       → {_wrap(job.summary, limit=200)}")
        for call in calls.get(correlation_id, []):
            step = f" — for step: {_wrap(call.plan_step, limit=120)}" if call.plan_step else ""
            # The agent beside the human, never instead: a helper's calls inside the chemist's turn
            # must be distinguishable from the chemist's own.
            via = f" via {call.agent}" if call.agent else ""
            stamp = f"{call.outcome}, {call.latency_ms:.0f} ms, {call.actor}{via}"
            lines.append(f"   tool {call.tool} [{stamp}]{step}")
        # Last, after everything the turn did, because that is when it ended.
        if correlation_id in endings:
            lines.append(endings[correlation_id].line())
        lines.append("")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: `python -m chemclaw.cli.explain <session-id>`.

    Arguments are declared, so flags are not looked up as session ids. A blank id is refused: it
    would match rows written before `session_id` was recorded and print other actors' audit rows. An
    unreachable database or one without this schema is reported in one line with a remedy; anything
    else still raises, since a bug here is not the database refusing.
    """
    parser = argparse.ArgumentParser(
        prog="python -m chemclaw.cli.explain",
        description="Reconstruct a session: its turns, the tools each ran, and why.",
    )
    parser.add_argument("session_id", help="the session id to reconstruct.")
    options = parser.parse_args(argv)
    if not options.session_id.strip():
        print(
            "session id must be a non-empty id; refusing to reconstruct on a blank id, which "
            "matches the rows written before the correlation id existed.",
            file=sys.stderr,
        )
        return 64
    try:
        lines = asyncio.run(explain(options.session_id))
    except ConnectionError as exc:
        # `core.db` raises `ConnectionError` (here `_DatabaseUnavailable`) for an unreachable or
        # saturated database; caught by that documented contract.
        print(f"cannot read the audit trail: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # A reachable database without this schema raises e.g. `UndefinedTable`, which `core/db`
        # does not map; the likeliest way an auditor runs this wrong, so it gets one line too.
        if not _is_database_refusal(exc):
            raise
        print(f"cannot read the audit trail: {exc}", file=sys.stderr)
        print(
            "  the database answered but refused the query — most likely the wrong database, or "
            "one that has not been migrated. Check CHEMCLAW_POSTGRES_DSN and run "
            "`make db-migrate`.",
            file=sys.stderr,
        )
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
