"""Erase a departed person's conversational data, and keep every record GxP requires kept.

**The question this answers is "someone left, now what?"** Removing an Entra app role stops new
access immediately and is the whole of *authorization* offboarding — but it deletes nothing, and
this system stores per-actor rows in nine tables. Until this module there was no answer at all to
"remove their data", which is a question a regulated deployment will be asked and which nobody
should be answering with hand-written SQL at the time it is asked.

**The line is drawn at attribution, not at convenience**, and it is the same line the whole system
is built on. Two tiers:

- **Erasable — the conversation.** Sessions, their messages and events, the turn lease, personal
  preferences, watch subscriptions. This is how one person worked: private, revisable, of no
  interest to anyone else (`agent/preferences.py` makes the same argument for why a preference is
  not a knowledge note). Nothing here is evidence about the chemistry.
- **Retained — the record.** The audit trail, plan approvals, note proposals, BO suggestions, job
  records and turn costs. Each says *who did what to the science*, which is precisely what a GxP
  system exists to be able to say, and an attributable record that can be deleted on request is not
  an attributable record. `audit_events` additionally carries a tamper-evident hash chain
  (`make audit-verify`): deleting a row does not merely remove information, it breaks the proof
  that the surrounding rows were never altered.

So this command **reports both tiers** rather than silently doing half the job. An operator who
needs the retained tier addressed as well has a data-protection question that outranks a CLI flag,
and the report tells them exactly how many rows and in which tables, so the conversation starts from
a number. Withholding that count would make a partial erasure look like a complete one — the one
outcome worse than refusing.

Erasure is **irreversible and identity-scoped**, so the caller states the actor exactly; there is no
pattern match and no "all users matching". The default is a dry run.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

import psycopg

from chemclaw.agent.checkpointer import CHECKPOINT_TABLES
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError

logger = logging.getLogger(__name__)


class ErasureError(ChemclawError):
    """An erasure cannot proceed: a blank actor, or a statement the database refused.

    A `ChemclawError` (so a `ValueError`) like `ConnectorError` and `DataSourceError`, so the one
    `except ValueError` at an entry point catches every "this deployment is misconfigured" failure
    regardless of which seam raised it — and so `chemclaw.cli.erase_actor` needs no import of the
    database driver to report one.
    """


# The conversational tier, deleted in dependency order: rows keyed by `session_id` go before the
# `session_owners` rows that are the only way to find which sessions were theirs. Reversing this
# would orphan every message rather than remove it.
#
# Written as (table, SQL) pairs rather than as one statement so the report can attribute a count to
# a table an operator can go and look at. `session_messages`, `session_events` and `session_turns`
# carry no actor of their own — they are reached through the ownership table, which is why that one
# is deleted last.
_SESSION_SCOPED = "SELECT session_id FROM session_owners WHERE owner = %(actor)s"
# The LangGraph checkpointer holds the same conversation as graph state, keyed by `thread_id` —
# which is the session id. Erasing `session_messages` and leaving these behind would remove a
# departing person's transcript while their turn state, tool calls and results stayed readable, and
# the sweep would report success. They are erased in the same session-scoped pass, before
# `session_owners` goes, for exactly the reason the tables above are.
#
# **Skipped when absent, and the skip has to happen before the statement is sent.** These tables are
# created by `AsyncPostgresSaver.setup()` rather than by a migration in `infra/sql`, so a deployment
# that has never run the LangGraph engine does not have them — and erasure must not become the one
# operation such a deployment cannot perform.
#
# A `WHERE to_regclass(...) IS NOT NULL` guard inside the statement was the first attempt and does
# not work: Postgres resolves `DELETE FROM checkpoints` at *parse* time, so the guard never gets
# evaluated and the whole erasure fails with `relation "checkpoints" does not exist`. Measured
# against a schema with no checkpointer, which is exactly what every current deployment is. Hence
# `_existing_tables` below — the check has to be a separate query.
_CHECKPOINT_ERASE: tuple[tuple[str, str], ...] = tuple(
    (table, f"DELETE FROM {table} WHERE thread_id IN ({_SESSION_SCOPED})")
    for table in CHECKPOINT_TABLES
)
_ERASE: tuple[tuple[str, str], ...] = (
    ("session_messages", f"DELETE FROM session_messages WHERE session_id IN ({_SESSION_SCOPED})"),
    *_CHECKPOINT_ERASE,
    ("session_events", f"DELETE FROM session_events WHERE session_id IN ({_SESSION_SCOPED})"),
    # `holder` as well as the session scope: a turn lease names the actor holding it, and releasing
    # a departed person's lease is the point. The session-scoped half alone would leave a lease held
    # on a session whose ownership row had already gone in the same run.
    (
        "session_turns",
        f"DELETE FROM session_turns WHERE holder = %(actor)s OR session_id IN ({_SESSION_SCOPED})",
    ),
    ("subscriptions", "DELETE FROM subscriptions WHERE owner = %(actor)s"),
    ("user_preferences", "DELETE FROM user_preferences WHERE owner = %(actor)s"),
    ("session_owners", "DELETE FROM session_owners WHERE owner = %(actor)s"),
)

# The GxP tier: counted, named, never deleted. Each entry is (table, actor columns, why it stays).
#
# **Columns, plural, and that is not defensive generality.** `note_proposals` carries two — `actor`
# (who proposed) and `decided_by` (who reviewed) — so a table-to-column mapping could only ever
# report one of them. The first version of this listed six single columns and missed both
# `note_proposals.decided_by` and `bo_campaigns.opened_by`, which meant a departing *reviewer* was
# told zero `note_proposals` rows mentioned them while their oid sat in the column that records
# every sign-off they gave. `tests/test_leaver.py` now derives this set from the live schema, so the
# next column added to any of these tables fails a test rather than going silently unreported.
_RETAINED: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "audit_events",
        ("actor",),
        "the GxP trail, and a tamper-evident hash chain — a deletion breaks the proof over the "
        "rows either side of it, not just the row removed (see `make audit-verify`)",
    ),
    ("plan_approvals", ("actor",), "who approved a plan before it was allowed to spend anything"),
    (
        "note_proposals",
        ("actor", "decided_by"),
        "who proposed a knowledge note, and who signed it off at the PR-gate — the second is the "
        "human half of 'AI proposes, human disposes' and is the whole reason the gate is auditable",
    ),
    ("bo_suggestions", ("actor",), "who a campaign's recommendation was made for"),
    ("bo_campaigns", ("opened_by",), "who framed an optimization campaign's decision space"),
    ("job_records", ("requested_by",), "who requested a durable calculation"),
    ("turn_costs", ("actor",), "what a person's turns cost, the record an operator bills against"),
)


@dataclass
class ErasureReport:
    """What was removed, what was deliberately kept, and whether anything was actually written."""

    actor: str
    applied: bool
    erased: dict[str, int] = field(default_factory=dict)
    retained: dict[str, int] = field(default_factory=dict)

    @property
    def erased_total(self) -> int:
        """How many conversational rows this run removed (or would remove, in a dry run)."""
        return sum(self.erased.values())

    @property
    def retained_total(self) -> int:
        """How many rows carry this actor and stay, because the record needs them."""
        return sum(self.retained.values())


async def _existing_tables(cur: Any, tables: set[str]) -> set[str]:
    """Which of `tables` exist on this connection's `search_path`.

    One query rather than a guard inside each statement, because a guard inside the statement
    cannot work: `DELETE FROM t` resolves `t` when the statement is *parsed*, long before any
    `WHERE` runs. Every table in `_ERASE` is checked, not just the checkpointer's, so the answer
    does not depend on remembering which ones might be missing.
    """
    await cur.execute(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind = 'r' AND c.relname = ANY(%s) "
        "AND n.nspname = ANY(current_schemas(true))",
        (sorted(tables),),
    )
    return {str(row[0]) for row in await cur.fetchall()}


async def erase_actor(actor: str, *, apply: bool = False) -> ErasureReport:
    """Count — and, with `apply`, delete — one actor's conversational rows.

    Runs in a single transaction, so a dry run and a real run see exactly the same rows and a
    failure part-way through leaves nothing half-erased. The dry run reaches the database rather
    than estimating: the number an operator signs off on has to be the number that will be deleted,
    and the only way to be sure of that is to have deleted it and rolled back.

    Args:
        actor: The Entra `oid` (or the configured dev actor id) whose data to erase. Matched
            exactly — there is no prefix, pattern or wildcard form, because the blast radius of
            getting one wrong is unrecoverable.
        apply: Commit the deletion. The default counts and rolls back.

    Returns:
        The per-table counts for both tiers.

    Raises:
        ErasureError: `actor` is blank — which would otherwise match every row whose owner column
            is empty, and those are the un-attributed rows of a dev deployment, not one person's —
            or the database refused a statement (see below).
    """
    if not actor.strip():
        raise ErasureError("actor must be a non-empty id; refusing to erase on a blank actor")

    report = ErasureReport(actor=actor, applied=apply)
    try:
        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                for table, columns, _ in _RETAINED:
                    # One row counts once however many of its columns name this actor: a proposal
                    # they both wrote and reviewed is one retained record, not two.
                    predicate = " OR ".join(f"{column} = %(actor)s" for column in columns)
                    await cur.execute(
                        f"SELECT count(*) FROM {table} WHERE {predicate}", {"actor": actor}
                    )
                    row = await cur.fetchone()
                    report.retained[table] = int(row[0]) if row else 0
                present = await _existing_tables(cur, {table for table, _ in _ERASE})
                for table, statement in _ERASE:
                    if table not in present:
                        # Reported as zero rather than omitted: "this deployment holds none of your
                        # rows there" is true, and a report whose keys vary by deployment is one an
                        # operator cannot compare against another run.
                        report.erased[table] = 0
                        continue
                    await cur.execute(statement, {"actor": actor})
                    report.erased[table] = cur.rowcount if cur.rowcount > 0 else 0
            if apply:
                await conn.commit()
            else:
                # The whole point of the dry run: the deletes above really ran, so the counts are
                # the database's answer rather than a second query hoping to predict it.
                await conn.rollback()
    except psycopg.Error as exc:
        # Translated here rather than caught in the CLI, for two reasons that point the same way.
        # `core.db` already turns an unreachable server into `ConnectionError`, so what reaches this
        # is a *statement* refusal — overwhelmingly `InsufficientPrivilege`, what a deployment gets
        # when `make db-grants` has not been re-applied for this command's own
        # `DELETE ON session_owners`. And the entry point that reports it is `chemclaw.cli`, which
        # `tests/test_third_party_layering.py` forbids from importing a database driver at all: a
        # terminal entry point should not know which driver is underneath. Both are answered by
        # raising this seam's own error, which is a `ValueError` like every other
        # "this deployment is misconfigured" failure in the codebase.
        raise ErasureError(f"the database refused the erasure: {exc}") from exc

    logger.info(
        "erasure %s for actor: %d conversational row(s) across %d table(s); "
        "%d attributed row(s) retained",
        "applied" if apply else "previewed",
        report.erased_total,
        len([t for t, n in report.erased.items() if n]),
        report.retained_total,
    )
    return report


def retention_reasons() -> tuple[tuple[str, str], ...]:
    """(table, why it is retained) for every GxP table, so a report can print the reason.

    Exposed rather than inlined into the CLI's formatter because the reason is the substantive part
    of the answer: an operator asking "why is this row still here?" should read it from the module
    that decided, not from a print statement.
    """
    return tuple((table, reason) for table, _, reason in _RETAINED)
