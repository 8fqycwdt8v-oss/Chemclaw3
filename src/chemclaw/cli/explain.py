"""Reconstruct why a session's tool calls happened — the join made usable.

The join is D-2026-07-31-the-audit-chain-is-versioned.

`audit_events.session_id` and `session_messages.correlation_id` are two columns; on their own they
are a schema change nobody would notice. This is what they are *for*: given a session, print the
conversation and, under each turn, the tools that ran because of it — with a durable job's stated
rationale where one exists (D-157).

The question this answers is the one a reviewer and a chemist ask in the same words: **"why was
this run?"** Before the join it was answerable for durable jobs and for nothing else, because a
tool call recorded its arguments and its actor and had no key back to the conversation.

Read-only by construction: three `SELECT`s and no writes. Deliberately not an agent tool — the
audit trail is evidence *about* the agent, and a surface that let the agent read its own trail
would invite it to summarize rather than to be examined.
"""

import asyncio
import sys
from typing import NamedTuple

from chemclaw.agent.session_store import is_degraded_render, message_from_row
from chemclaw.api.schemas import message_role, message_text
from chemclaw.core.config import settings
from chemclaw.core.db import connection

# One turn's words, in order. `created_at` disambiguates rows written before `correlation_id`
# existed (they carry '' and collapse into a single "unattributed" group).
_MESSAGES = """
    SELECT correlation_id, message, message_shape, created_at
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


def _speaker(message: object, shape: str | None = None) -> tuple[str, str]:
    """The `(role, text)` of a stored message, tolerating shapes this tool did not write.

    **Read through `session_store.message_from_row`, not parsed here.** `session_messages` holds
    two serializations — the framework layer 1 was first built on wrote one, LangChain writes the
    other, and the M6 conversion pass is resumable so a real table holds both indefinitely. This
    function used to parse the legacy one inline, which meant every row written after that
    conversion rendered blank: an audit reconstruction that silently shows an empty conversation is
    worse than one that fails, because it looks like nothing was said.

    A reconstruction tool must still not fail on a message it cannot parse — the row is evidence
    that *something* was said — so an unreadable payload renders as its repr under an `unknown`
    role rather than raising.

    **A row the store could only *recover* gets that same `unknown` role**, because that is what it
    is. `message_from_row` never raises: a payload it cannot convert comes back as prose under a
    speaker guessed from whichever label the row happens to carry, which is the right answer for a
    chemist reloading a conversation and the wrong one here — an audit reconstruction that prints a
    guessed speaker as the record is a report nobody can tell apart from a true one. The store
    stamps what it recovered (`is_degraded_render`); this is the reader that acts on it.

    **The repr fallback is reached by an empty render, not only by an exception**, which is what
    made the promise above true rather than merely written down. `message_from_row` catches
    internally and returns a degraded message rather than raising, so the `except` arm below is
    dead for a dict payload — and a payload with no recoverable prose came back as an *empty*
    message, which `explain` then dropped with `if text:`. The turn rendered "transcript: absent
    (compacted, pruned, or rolled back)": a specific, and wrong, explanation of a row that is on
    disk and simply unreadable. `session_messages.message` is bare `jsonb`, so a shape neither
    reader recognises is the case this exists for. It applies to the *degraded* branch only: a row
    the store read fine and which genuinely holds no prose — a legacy image part — keeps its
    speaker and its emptiness, because that is a different fact.
    """
    if not isinstance(message, dict):
        return "unknown", str(message)
    try:
        restored = message_from_row(message, shape)
    except Exception:
        return "unknown", str(message)
    if is_degraded_render(restored):
        return "unknown", message_text(restored).strip() or str(message)
    # Rendered by the transcript route's own projection, not a second one: a conversation that
    # reads `assistant` in the browser and `ai` here would make one turn look like two records.
    # A *readable* row with no prose keeps its role and its emptiness: a legacy row carrying only
    # an image part has a speaker and nothing to say, which is a different fact from a row nothing
    # could render, and only the second is worth printing as a repr.
    return message_role(restored), message_text(restored).strip()


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
    hypothetical — `durable/retention.py` prunes message rows by age, and a turn that ran its
    tools and then failed or was abandoned never writes a transcript row in the first place (the
    projection is written once, after the answer). So the trail routinely outlives the words it
    points at. Dropping those turns would hide the evidence
    an auditor most wants.

    **One insertion-ordered pass over all three, so a turn is shown once.** The de-duplication used
    to exclude only ids already in `order`, and `(*calls, *jobs)` concatenates two key sequences
    without comparing them to each other — so exactly the turn this docstring describes, one with
    both a tool call and a durable job and no surviving transcript row, was rendered twice. Same
    header, same lines, one occurrence read as two.
    """
    shown = list(dict.fromkeys([*order, *calls, *jobs]))
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
