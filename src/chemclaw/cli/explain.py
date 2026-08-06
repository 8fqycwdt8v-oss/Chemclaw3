"""Reconstruct why a session's tool calls happened — the join made usable.

The join is D-2026-07-31-the-audit-chain-is-versioned.

`audit_events.session_id` and `session_messages.correlation_id` are two columns; on their own they
are a schema change nobody would notice. This is what they are *for*: given a session, print the
conversation and, under each turn, the tools that ran because of it — with a durable job's stated
rationale where one exists (D-157).

The question this answers is the one a GxP auditor and a chemist ask in the same words: **"why was
this run?"** Before the join it was answerable for durable jobs and for nothing else, because a
tool call recorded its arguments and its actor and had no key back to the conversation.

Read-only by construction: three `SELECT`s and no writes. Deliberately not an agent tool — the
audit trail is evidence *about* the agent, and a surface that let the agent read its own trail
would invite it to summarize rather than to be examined.
"""

import asyncio
import sys
from typing import NamedTuple

from chemclaw.core.config import settings
from chemclaw.core.db import bounded

# One turn's words, in order. `created_at` disambiguates rows written before `correlation_id`
# existed (they carry '' and collapse into a single "unattributed" group).
_MESSAGES = """
    SELECT correlation_id, message, created_at
    FROM session_messages
    WHERE session_id = %s
    ORDER BY id ASC
"""

_AUDIT = """
    SELECT correlation_id, tool, outcome, detail, latency_ms, actor, purpose
    FROM audit_events
    WHERE session_id = %s
    ORDER BY id ASC
"""

# A durable job states its reason outright (D-157), so where one exists it is the best answer this
# tool can give and is printed verbatim rather than paraphrased.
_JOBS = """
    SELECT correlation_id, connector, job, rationale, summary
    FROM job_records
    WHERE session_id = %s
    ORDER BY completed_at ASC
"""


class ToolCall(NamedTuple):
    """One audited tool invocation as this report shows it."""

    tool: str
    outcome: str
    detail: str
    latency_ms: float
    actor: str
    purpose: str


class Job(NamedTuple):
    """One finished durable job, with the reason its launcher was required to state."""

    connector: str
    job: str
    rationale: str
    summary: str


def _speaker(message: object) -> tuple[str, str]:
    """The `(role, text)` of a stored MAF message, tolerating shapes this tool did not write.

    `session_messages.message` is whatever `Message.to_dict()` produced at the time, and that shape
    is upstream's to change. A reconstruction tool must not fail on a message it cannot parse —
    the row is still evidence that *something* was said, and saying so is more useful than a
    traceback. Unknown shapes render as their repr under an `unknown` role.
    """
    if not isinstance(message, dict):
        return "unknown", str(message)
    role = str(message.get("role", "unknown"))
    contents = message.get("contents")
    if isinstance(contents, list):
        parts = [
            str(item.get("text", ""))
            for item in contents
            if isinstance(item, dict) and item.get("text")
        ]
        if parts:
            return role, " ".join(parts)
    text = message.get("text")
    return role, str(text) if text else ""


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

    async with bounded(target) as conn:
        cursor = await conn.execute(_MESSAGES, (session_id,))
        for correlation_id, message, _created in await cursor.fetchall():
            if correlation_id not in turns:
                turns[correlation_id] = []
                order.append(correlation_id)
            role, text = _speaker(message)
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
            purpose,
        ) in await cursor.fetchall():
            calls.setdefault(correlation_id, []).append(
                ToolCall(tool, outcome, detail, latency, actor, purpose)
            )

        # `job_records` predates this join and is keyed independently, so a job whose correlation id
        # is absent still belongs to the session and must not be dropped.
        cursor = await conn.execute(_JOBS, (session_id,))
        for correlation_id, connector, job, rationale, summary in await cursor.fetchall():
            jobs.setdefault(correlation_id, []).append(Job(connector, job, rationale, summary))

    return _render(session_id, order, turns, calls, jobs)


def _render(
    session_id: str,
    order: list[str],
    turns: dict[str, list[tuple[str, str]]],
    calls: dict[str, list[ToolCall]],
    jobs: dict[str, list[Job]],
) -> list[str]:
    """Format the gathered rows; separated from the fetch so it is testable without a database.

    Which turns to show is decided here rather than during the fetch, and that placement is the
    reason this is testable at all: a turn the audit trail knows about but the transcript does not
    is still shown, and that rule is exactly the one worth exercising offline. It is not
    hypothetical — `session_store._compact` rewrites message rows, retention prunes them, and
    `rollback_to` deletes a turn's on disconnect, so the trail routinely outlives the words it
    points at. Dropping those turns would hide the evidence an auditor most wants.
    """
    shown = [*order, *(cid for cid in (*calls, *jobs) if cid not in set(order))]
    lines = [f"session {session_id}", ""]
    if not shown:
        lines.append("  no messages, tool calls or jobs recorded for this session")
        return lines
    for correlation_id in shown:
        label = correlation_id or "(unattributed — written before the correlation id was recorded)"
        lines.append(f"── turn {label}")
        said = turns.get(correlation_id, [])
        if not said:
            lines.append("   transcript: absent (compacted, pruned, or rolled back)")
        for role, text in said:
            lines.append(f"   {role}: {_wrap(text)}")
        for job in jobs.get(correlation_id, []):
            lines.append(f"   job {job.connector}:{job.job} — because: {_wrap(job.rationale)}")
            lines.append(f"       → {_wrap(job.summary, limit=200)}")
        for call in calls.get(correlation_id, []):
            reason = f" — because: {_wrap(call.purpose, limit=120)}" if call.purpose else ""
            stamp = f"{call.outcome}, {call.latency_ms:.0f} ms, {call.actor}"
            lines.append(f"   tool {call.tool} [{stamp}]{reason}")
        lines.append("")
    return lines


def main() -> int:
    """CLI: `python -m chemclaw.cli.explain <session-id>`."""
    if len(sys.argv) != 2:
        print("usage: python -m chemclaw.cli.explain <session-id>", file=sys.stderr)
        return 64
    for line in asyncio.run(explain(sys.argv[1])):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
