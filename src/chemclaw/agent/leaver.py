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

from chemclaw.core import db
from chemclaw.core.config import settings

logger = logging.getLogger(__name__)

# The conversational tier, deleted in dependency order: rows keyed by `session_id` go before the
# `session_owners` rows that are the only way to find which sessions were theirs. Reversing this
# would orphan every message rather than remove it.
#
# Written as (table, SQL) pairs rather than as one statement so the report can attribute a count to
# a table an operator can go and look at. `session_messages`, `session_events` and `session_turns`
# carry no actor of their own — they are reached through the ownership table, which is why that one
# is deleted last.
_SESSION_SCOPED = "SELECT session_id FROM session_owners WHERE owner = %(actor)s"
_ERASE: tuple[tuple[str, str], ...] = (
    ("session_messages", f"DELETE FROM session_messages WHERE session_id IN ({_SESSION_SCOPED})"),
    ("session_events", f"DELETE FROM session_events WHERE session_id IN ({_SESSION_SCOPED})"),
    ("session_turns", f"DELETE FROM session_turns WHERE session_id IN ({_SESSION_SCOPED})"),
    ("subscriptions", "DELETE FROM subscriptions WHERE owner = %(actor)s"),
    ("user_preferences", "DELETE FROM user_preferences WHERE owner = %(actor)s"),
    ("session_owners", "DELETE FROM session_owners WHERE owner = %(actor)s"),
)

# The GxP tier: counted, named, never deleted. Each entry is (table, actor column, why it stays).
_RETAINED: tuple[tuple[str, str, str], ...] = (
    (
        "audit_events",
        "actor",
        "the GxP trail, and a tamper-evident hash chain — a deletion breaks the proof over the "
        "rows either side of it, not just the row removed (see `make audit-verify`)",
    ),
    ("plan_approvals", "actor", "who approved a plan before it was allowed to spend anything"),
    ("note_proposals", "actor", "who proposed a knowledge note the PR-gate then reviewed"),
    ("bo_suggestions", "actor", "who a campaign's recommendation was made for"),
    ("job_records", "requested_by", "who requested a durable calculation"),
    ("turn_costs", "actor", "what a person's turns cost, the record an operator bills against"),
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
        ValueError: `actor` is blank — which would otherwise match every row whose owner column is
            empty, and those are the un-attributed rows of a dev deployment, not one person's.
    """
    if not actor.strip():
        raise ValueError("actor must be a non-empty id; refusing to erase on a blank actor")

    report = ErasureReport(actor=actor, applied=apply)
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            for table, column, _ in _RETAINED:
                await cur.execute(
                    f"SELECT count(*) FROM {table} WHERE {column} = %(actor)s", {"actor": actor}
                )
                row = await cur.fetchone()
                report.retained[table] = int(row[0]) if row else 0
            for table, statement in _ERASE:
                await cur.execute(statement, {"actor": actor})
                report.erased[table] = cur.rowcount if cur.rowcount > 0 else 0
        if apply:
            await conn.commit()
        else:
            # The whole point of the dry run: the deletes above really ran, so the counts are the
            # database's answer rather than a second query hoping to predict it.
            await conn.rollback()

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
