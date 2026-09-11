"""Reconstruct why a session's tool calls happened — the join made usable.

The join is D-2026-07-31-the-audit-chain-is-versioned.

`audit_events.session_id` and `session_messages.correlation_id` are two columns; on their own they
are a schema change nobody would notice. This is what they are *for*: given a session, print the
conversation and, under each turn, the tools that ran because of it — with a durable job's stated
rationale where one exists (D-157).

The question this answers is the one a reviewer and a chemist ask in the same words: **"why was
this run?"** Before the join it was answerable for durable jobs and for nothing else, because a
tool call recorded its arguments and its actor and had no key back to the conversation.

A tool call now also names **which plan step it served** (`audit_events.plan_step`,
`D-2026-08-27-a-refusal-is-not-a-crash`), which is the nearest thing to a stated reason that can be
recorded without inventing one — including for a call a gate *refused*, where no job exists to carry
the rationale.

Read-only by construction: five `SELECT`s and no writes. The fifth is `turn_costs`, and it is
the one that stops a degraded turn reading as a clean one: a turn stopped by its model-call cap
left the front door with `event: error … "code":"loop_cap_reached"` on the wire and this
reconstruction identical to a complete turn's, because the four queries above ask what was *said*
and what *ran* and nothing asked how the turn ended. Deliberately not an agent tool — the
audit trail is evidence *about* the agent, and a surface that let the agent read its own trail
would invite it to summarize rather than to be examined.
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

# One turn's words, in order. `created_at` disambiguates rows written before `correlation_id`
# existed (they carry '' and collapse into a single "unattributed" group).
_MESSAGES = """
    SELECT correlation_id, message, message_shape, created_at
    FROM session_messages
    WHERE session_id = %s
    ORDER BY id ASC
"""

# `plan_step` rather than `purpose`, and the swap is the point. `purpose` is "why this call was
# made, in the requester's terms" and has been empty on every row ever written — nothing can author
# it honestly (see `agent/audit.AuditEvent.purpose`), so the operator tool's "why" column was
# *structurally* blank while looking like a column that sometimes has content. `plan_step` is a
# narrower question with an exact answer: the plan step in flight when the call was made. Rendering
# the answerable one is better than rendering the honest blank, and copying `plan_step` into
# `purpose` would have been the inference that field refuses to be.
#
# Ordered by `ts` before `id`, because `id` is a `BIGSERIAL` assigned when the batching sink
# *flushes* — so under load the reconstruction told the flusher's story rather than the turn's.
# `id` stays as the tiebreak: two calls can start inside the same clock tick, and insertion order
# is the only thing left that distinguishes them.
_AUDIT = """
    SELECT correlation_id, tool, outcome, detail, latency_ms, actor, plan_step, agent
    FROM audit_events
    WHERE session_id = %s
    ORDER BY ts ASC, id ASC
"""

# A durable job states its reason outright (D-157), so where one exists it is the best answer this
# tool can give and is printed verbatim rather than paraphrased.
_JOBS = """
    SELECT correlation_id, connector, job, rationale, summary
    FROM job_records
    WHERE session_id = %s
    ORDER BY completed_at ASC
"""

# **How each turn ended, which is the fact this report was missing.** `turn_costs` is keyed on
# exactly the id every group above is keyed on, and until this query existed the reconstruction of
# a turn stopped by its model-call cap was byte-identical to a clean one — measured on a live
# database, the two sessions differed only in the assistant's own sentence:
#
#     CLEAN       turn_costs: ('cc9738df…', 'answered',    True, None, False)
#     LOOP-CAPPED turn_costs: ('7b62ed3a…', 'loop_capped', True, None, False)
#
# The columns are the ones that say something about the *answer* rather than about its price:
# `outcome` and `completed` (migration 060), `compacted` and `context_unreducible` (069),
# `answer_confidence` and `review_required` (082-083). Spend is deliberately not read — that is
# `operations.activity.spend`'s question, and this report is about why a call happened and what
# came of it.
#
# Ordered so the *last* row for a correlation id wins, which matters for the one writer that books
# more than one row under a run's id: a template's `agent` steps suffix the step onto the
# correlation id (`durable/template_activities._book_step_spend`), so a prefix is not read here at
# all and each step's row stands on its own.
_COSTS = """
    SELECT correlation_id, outcome, completed, compacted, context_unreducible,
           answer_confidence, review_required, error_code
    FROM turn_costs
    WHERE session_id = %s
    ORDER BY recorded_at ASC
"""

# Whether the session was ever created here at all. One row is written per session at creation
# (`infra/sql/013_session_owners.sql`), so its presence is the only thing in this database that can
# tell a mistyped id from a session that ran and left nothing — and telling those apart is the whole
# of what an empty reconstruction was missing. Read for that message alone: nothing else in this
# report needs it, and the owner itself is deliberately not printed, since this is a reconstruction
# of a session's *work* and the actor is already on every row that has one.
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
    #: talking to (`agent/audit.AuditEvent.agent`). Rendered only when non-empty, because empty is
    #: the overwhelming majority and "via the agent you were talking to" is noise on every line.
    #: Defaulted so a caller constructing this positionally — every test in `tests/test_explain.py`
    #: — keeps working; the fetch below always supplies it.
    agent: str = ""


class Job(NamedTuple):
    """One finished durable job, with the reason its launcher was required to state."""

    connector: str
    job: str
    rationale: str
    summary: str


class TurnEnd(NamedTuple):
    """How one turn ended, from `turn_costs` — the fact this reconstruction had no query for.

    Every field is a *stored* fact rather than an inference, which is the whole reason this row
    can be printed as a record: the front door's SSE wire tells a live client a turn was capped
    (`event: error … "code":"loop_cap_reached"`), and nothing carried that forward, so a reload and
    this report showed a capped turn as a finished one. The row was always there.

    All defaulted so a caller constructing one positionally in a test keeps working, and because
    the columns arrived in four migrations (060, 069, 082, 083) — a row written before any of them
    carries that column's default, and `outcome='unknown'` in particular means "written before the
    column existed" rather than "ended in some unknown way".
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

        `answered` with nothing else to say still prints, and that is deliberate: the value of a
        marker is that its *absence* means something, and a line only rendered when something went
        wrong would leave a reader unable to tell "this turn ended cleanly" from "this deployment
        does not record how turns end".
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


def _why_nothing(known: bool) -> list[str]:
    """Why this reconstruction is empty — the half that made an empty one useless.

    **`explain <a session that just ran a tool>` and `explain <an id that never existed>` printed
    the identical line.** That is worth little on its own and it is the *symptom* of the defect
    above it: on the shipped configuration nothing is written for any session, so the two really
    are the same state and the report was right to be unable to tell them apart. What it could
    have said, and did not, is which of the three situations a reader is in — and two of the three
    are things the reader can act on.

    Under `session_store="memory"` no id can be distinguished from any other, so this says so and
    names the setting rather than implying the session is unknown. Under `postgres`,
    `session_owners` holds one row per session from its creation, so an id with no row was never
    created here (a typo, another deployment, another database) and an id with a row ran and left
    nothing — which is retention, an abandoned turn, or a session that never took one.
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

    `chemclaw.cli` may not import `psycopg` — `tests/test_third_party_layering.py` allows the
    `postgres` stack to `chemclaw.core` alone, because `core/db.py` is the one connection pool —
    so this cannot name `psycopg.Error`. What it can do is ask for the two attributes every
    `psycopg.Error` carries and nothing else in this process does: `sqlstate`, the five-character
    SQLSTATE the *server* returned, and `diag`, its diagnostics. A `ValueError` has neither.

    Duck-typed rather than string-matched on the module name deliberately: a name check would pass
    for any class defined in a module that happens to start with "psycopg", and would fail the day
    the driver is wrapped. The attribute pair is the driver's own documented surface.

    The right long-term home is `core/db`, which already publishes the contract this works around
    ("an unreachable or saturated database raises `ConnectionError`") and maps only
    `OperationalError` onto it. Widening that mapping is a change to a module every layer imports,
    so it is a decision of its own; this keeps the promise `main`'s docstring already makes.
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

        # `job_records` predates this join and is keyed independently, so a job whose correlation id
        # is absent still belongs to the session and must not be dropped.
        cursor = await conn.execute(_JOBS, (session_id,))
        for correlation_id, connector, job, rationale, summary in await cursor.fetchall():
            jobs.setdefault(correlation_id, []).append(Job(connector, job, rationale, summary))

        cursor = await conn.execute(_COSTS, (session_id,))
        for correlation_id, *fields in await cursor.fetchall():
            # Last row wins for a correlation id, which is the ordering's job — the ledger upserts
            # on `turn_id` rather than on this key, so a re-booked turn is one row and a template's
            # steps are several under suffixed ids.
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
    endings = ends or {}
    # **`endings` is a fourth source of turns, not only an annotation on the other three.** A turn
    # that was cancelled writes a cost row and nothing else — no transcript projection, no audit
    # row — so it used to fall through to `_why_nothing`, which offered the reader three guesses
    # ("retention has pruned it, its turns were abandoned…, or it never took a turn") while the
    # ledger held `abandoned` on exactly the key this function groups by. A stored fact must not be
    # rendered as a guess.
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
            # **The ledger is read before the guess, because it is one line below.** This printed
            # three named causes — compacted, pruned, rolled back — directly above its own
            # `ended: errored` line, so an operator following the display investigated compaction,
            # retention and a rollback and found all three healthy. A turn that errored or was
            # abandoned before its transcript row was written is by far the commonest way to reach
            # here, and `endings` already holds that on exactly the key this loop is iterating.
            #
            # The same string was corrected once before for the *unreadable-row* case (see this
            # module's docstring); the *absent-row* case kept the wrong leads. A stored fact must
            # not be rendered as a guess, which is the rule the `endings` source exists for.
            ending = endings.get(correlation_id)
            if ending is not None and ending.outcome != "answered":
                lines.append(
                    f"   transcript: absent (the turn ended {ending.outcome} "
                    "before one was written)"
                )
            else:
                lines.append("   transcript: absent (compacted, pruned, or rolled back)")
        for role, text in said:
            # **A tool's stored result is not a speaker, and it read as one.** A `ToolMessage` is
            # in `session_messages` like every other message, so `message_role` called it `tool`
            # and it printed as `tool: {'flags': [...]}` — a payload rendered as transcript speech,
            # directly above the audit trail's own `tool screen_hazards [ok, 42 ms, …]` line for
            # the very same call. One call, two renderings, the first of which invited a reader to
            # mistake a result for something said. It is still printed, because the audit row
            # records that a call happened and never what it returned; it is printed as what it is.
            label = "tool result" if role == "tool" else role
            lines.append(f"   {label}: {_wrap(text)}")
        for job in jobs.get(correlation_id, []):
            lines.append(f"   job {job.connector}:{job.job} — because: {_wrap(job.rationale)}")
            lines.append(f"       → {_wrap(job.summary, limit=200)}")
        for call in calls.get(correlation_id, []):
            step = f" — for step: {_wrap(call.plan_step, limit=120)}" if call.plan_step else ""
            # The agent beside the human, never instead of it
            # (`D-2026-09-06-the-one-agent-that-exists-is-named-in-the-trail`). A helper runs on a
            # brief the chemist never saw, so a row of its own inside the chemist's turn is exactly
            # what a reviewer needs told apart — and it read as the chemist's own act until the
            # column had a producer.
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

    **Declared rather than read raw.** `sys.argv[1]` was taken as a session id whatever it was, so
    `--help` and `--not-a-flag` were both looked up as sessions and both exited 0 under a heading
    naming the flag — the identical defect `validate_kg`'s docstring records as fixed, still live
    in the tool an auditor reaches for. Worse than a mis-read argument: the empty string matched
    the rows written before `session_id` was recorded and printed *another* actor's audit rows and
    durable jobs under a blank heading, so a blank shell variable was a small disclosure. A blank
    id is refused for the reason `erase_actor` refuses one ("actor must be a non-empty id"), and
    the database's own unreachability is one line rather than a traceback, because an operator
    reading a stack trace out of a read-only reporting command learns only that it crashed.

    **That last sentence was true of one failure and read as being about all of them.** `core/db`
    maps `psycopg.OperationalError` onto `ConnectionError` and nothing else, so a database that is
    reachable but has no schema — the likeliest way an auditor runs this command wrong — came back
    as 29 lines of `psycopg.errors.UndefinedTable`. Both families are now one line plus a remedy;
    everything else still raises, because a bug in this module is not the database refusing.
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
        # `core.db` publishes "an unreachable or saturated database raises `ConnectionError`" and
        # `_DatabaseUnavailable` is that subclass, so this catches the real failure mode by its
        # documented contract rather than by a vendor exception type.
        print(f"cannot read the audit trail: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # **The docstring's promise held for exactly one failure.** `core/db` maps only
        # `psycopg.OperationalError` onto `ConnectionError`, and a database that is reachable but
        # does not have this schema raises `psycopg.errors.UndefinedTable`, a `ProgrammingError` —
        # so the branch above never saw it. Measured:
        #
        #     unmigrated / wrong schema:  EXIT=1, 29 stderr lines, psycopg.errors.UndefinedTable
        #     database unreachable:       EXIT=1, 4 lines, "cannot read the audit trail: …"
        #
        # A wrong DSN or an unmigrated database is the likeliest way an auditor runs this command
        # wrong, and `_why_nothing` already coaches them about the DSN on the other path.
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
