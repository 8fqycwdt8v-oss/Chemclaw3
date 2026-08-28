"""Erase a departed person's conversational data, and keep the record of what they did.

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
  records and turn costs. Each says *who did what to the science*, and an attributable record that
  can be deleted on request is not an attributable record: a result someone cites is only as good as
  the ability to say later who produced it and on whose authority.

So this command **reports both tiers** rather than silently doing half the job. An operator who
needs the retained tier addressed as well has a data-protection question that outranks a CLI flag,
and the report tells them exactly how many rows and in which tables, so the conversation starts from
a number. Withholding that count would make a partial erasure look like a complete one — the one
outcome worse than refusing.

Erasure is **irreversible and identity-scoped**, so the caller states the actor exactly; there is no
pattern match and no "all users matching" — but "exactly" means *the person*, not one spelling of
them, and this database holds two spellings of the same id (see `_actor_forms`). The default is a
dry run.
"""

import logging
from dataclasses import dataclass, field

import psycopg

from chemclaw.agent.checkpointer import CHECKPOINT_TABLES
from chemclaw.agent.scratchpad import memory_prefix
from chemclaw.agent.session_store import _session_dsn
from chemclaw.core import db
from chemclaw.core.db import existing_tables
from chemclaw.core.errors import ChemclawError
from chemclaw.durable.digest import digest_channel

logger = logging.getLogger(__name__)


class ErasureError(ChemclawError):
    """An erasure cannot proceed: a blank actor, or a statement the database refused.

    A `ChemclawError` (so a `ValueError`) like `ConnectorError` and `DataSourceError`, so the one
    `except ValueError` at an entry point catches every "this deployment is misconfigured" failure
    regardless of which seam raised it — and so `chemclaw.cli.erase_actor` needs no import of the
    database driver to report one.
    """


# The namespace a writer stamps onto an actor it was in no position to authenticate — a single,
# known, literal prefix, duplicated here from `connectors/bo/server/tools.py` because that module is
# a connector bundle and this one is core: importing it would make the erasure sweep depend on a
# bundle a deployment may not even enable. Two literals is the honest cost of that boundary; a third
# writer is the point at which the constant should move to a shared home (Rule of Three).
_UNVERIFIED_ACTOR_PREFIX = "unverified:"


def _actor_forms(actor: str) -> list[str]:
    """Every spelling this database legitimately holds of *one* person's id.

    **Why one id has two spellings.** A writer that cannot authenticate its caller records the
    claimed name marked as a claim: `connectors/bo/server/tools.py` stamps `unverified:` onto the
    actor from `X-Chemclaw-Actor` on the synchronous MCP path, because that bundle declares
    `auth: mode: none` and the header is attacker-writable. The durable sibling reads a validated
    principal off the run's memo and writes the bare id. So `bo_campaigns.opened_by` and
    `bo_suggestions.actor` — both in `_RETAINED` — hold `alice-oid` for one path and
    `unverified:alice-oid` for the other, and they are the same chemist. Matching only the bare form
    made a right-to-erasure report *under-count rows that still contain that person's identifier*,
    which is the one number this module exists to get right.

    **Exact equality against a closed set, never a pattern.** The forms are enumerated and compared
    with `=` (via `= ANY(...)`), so the guarantee the module is built on is unchanged: a row matches
    only if its column *is* one of these two strings. `LIKE '%' || actor || '%'` would have been one
    line and is the dangerous answer — it matches `oid-erik-2` when erasing `oid-erik`, i.e. deletes
    a different person's conversation and counts their retained records as the leaver's. Measured,
    not asserted: with that one-line version in place,
    `test_erasing_one_person_spares_another_whose_id_contains_theirs` reports 3 retained campaigns
    where 1 is the truth, and takes the bystander's sessions with it. A
    `LIKE actor || '%'` prefix match fails the same way; a `column LIKE 'unverified:%'` guard plus
    string surgery in SQL reimplements this function where it cannot be tested. The marker's prefix
    is literal and known, so the set is enumerable, and an enumerable set needs no pattern.

    **The caller may name either form.** An operator reads `unverified:alice-oid` out of the column
    or out of a previous report and pastes it; stripping the marker first makes both inputs mean the
    same person and keeps the set at two rather than growing an `unverified:unverified:` third.

    Applied to *both* tiers, not just to the two columns that hold a marked id today. The rule is
    "these strings name one person", which is a property of the id rather than of the table — the
    next `auth: mode: none` connector to write a person-column would otherwise silently escape the
    sweep, which is the failure this is fixing, one table over.
    """
    base = actor.removeprefix(_UNVERIFIED_ACTOR_PREFIX)
    return [base, f"{_UNVERIFIED_ACTOR_PREFIX}{base}"]


# The conversational tier, deleted in dependency order: rows keyed by `session_id` go before the
# `session_owners` rows that are the only way to find which sessions were theirs. Reversing this
# would orphan every message rather than remove it.
#
# Written as (table, SQL) pairs rather than as one statement so the report can attribute a count to
# a table an operator can go and look at. `session_messages`, `session_events` and `session_turns`
# carry no actor of their own — they are reached through the ownership table, which is why that one
# is deleted last.
#
# `= ANY(%(actors)s)` throughout rather than `= %(actor)s`: the parameter is the closed set of
# spellings of one id that `_actor_forms` returns, and `ANY` over an array is still exact equality
# against each element — the same comparison, applied to each form the same person's id can take.
_SESSION_SCOPED = "SELECT session_id FROM session_owners WHERE owner = ANY(%(actors)s)"
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
# `core.db.existing_tables` — the check has to be a separate query, which is also why the retention
# sweep shares it rather than owning a second copy.
_CHECKPOINT_ERASE: tuple[tuple[str, str], ...] = tuple(
    (table, f"DELETE FROM {table} WHERE thread_id IN ({_SESSION_SCOPED})")
    for table in CHECKPOINT_TABLES
)
# The agent's durable memories, which are **not** session-scoped and so cannot ride the pass above.
# A memory outlives the session it was written in — that is the whole point of it — so the only key
# that finds a departing person's is the one `agent/scratchpad.py` deliberately put in the store's
# namespace. `store.prefix` holds the dotted namespace, and `memory_prefix` builds the same string
# the writer wrote under rather than re-deriving the join here.
#
# **This is the row that makes the erasure check honest.**
# `D-2026-08-10-basestore-is-not-where-this-systems-memory-lives` rejected `BaseStore` partly
# because `store` has no actor column, so a derived completeness test would pass while a departing
# person's memories remained — *a safety net that returns a false green is worse than no safety
# net, because it is trusted*. Keying the namespace by actor is what answers that, and this is
# where the answer is spent.
#
# `store_vectors` before `store`: it has a foreign key on `(prefix, key)`, so the dependent side
# goes first for the same reason the session-scoped rows precede `session_owners`.
#
# Skipped when absent, for the reason `_CHECKPOINT_ERASE` is: `AsyncPostgresStore.setup()` creates
# these, not a migration, so a deployment that never enabled memories does not have them and
# erasure must not be the one operation it cannot perform.
_MEMORY_ERASE: tuple[tuple[str, str], ...] = (
    ("store_vectors", "DELETE FROM store_vectors WHERE prefix = ANY(%(memory_prefixes)s)"),
    ("store", "DELETE FROM store WHERE prefix = ANY(%(memory_prefixes)s)"),
)
_ERASE: tuple[tuple[str, str], ...] = (
    # **The full text of everything this person's tools returned.** Session-scoped rather than
    # actor-scoped, which is exactly why it was missed: `tests/test_leaver.py` derives completeness
    # from columns whose *name* identifies a person, and `tool_result_links` has none — its columns
    # are `session_id, content_hash, tool, correlation_id, created_at`. So a table holding a hazard
    # screen naming a chemist's compounds, an evidence sweep and a solvent ranking, in full
    # untruncated text, was invisible to the check while the report said the erasure was complete.
    #
    # **The blob is deleted and the link follows it**, rather than both being deleted here. The
    # link is what carries the session, so it is what selects the rows — but the DELETE is issued
    # against the blob, because `infra/sql/grants/app_privileges.sql` withholds DELETE on
    # `tool_result_links` on purpose: a cascade runs with the referencing table's owner privileges,
    # not the deleting role's, so "the sweep deletes blobs and links follow" is the *only* way a
    # link row can disappear. Deleting the link directly here would need that grant widened, which
    # is the boundary quietly moving to make an erasure convenient.
    (
        "tool_result_blobs",
        # **Only the blobs nobody else can still read.** The first arm finds what this person's
        # sessions linked; the second refuses to delete one that another session also links. Without
        # it, erasing one chemist deleted a *different* chemist's stored tool result — measured
        # against a live database: two sessions link one blob, `erase_actor` on the first removes
        # the blob, and `ON DELETE CASCADE` takes the second session's link row with it, leaving
        # that person's own transcript pointing at a result the surface can no longer fetch. The
        # arm is transcribed from `session_store._SESSION_DELETE`, which has had it since the
        # single-session delete was written; the two paths delete the same rows for the same
        # reason and only one of them knew it.
        #
        # A link whose session has *no* ownership row — the orphan `delete_session` leaves behind
        # when a blob is shared — reads as "somebody else still links this" and spares the blob.
        # That is the conservative direction on purpose: such a link names a session id that no
        # longer resolves to a person, so what it keeps alive is unattributable rather than
        # somebody's, and `durable/retention.py`'s age sweep collects both together.
        "DELETE FROM tool_result_blobs b WHERE EXISTS ("
        "  SELECT 1 FROM tool_result_links l"
        f"   WHERE l.content_hash = b.content_hash AND l.session_id IN ({_SESSION_SCOPED})"
        ") AND NOT EXISTS ("
        "  SELECT 1 FROM tool_result_links l"
        "   WHERE l.content_hash = b.content_hash"
        f"     AND l.session_id NOT IN ({_SESSION_SCOPED}))",
    ),
    ("session_messages", f"DELETE FROM session_messages WHERE session_id IN ({_SESSION_SCOPED})"),
    *_CHECKPOINT_ERASE,
    *_MEMORY_ERASE,
    # **Two kinds of session id reach this table, and the join only ever found one of them.**
    # A digest lands in the synthetic mailbox `digest-<oid>`, which by design has no
    # `session_owners` row (`durable/digest.digest_channel`) — so the reachability join below could
    # not match it, and
    # a departing person's unread digests survived an erasure that reported `session_events: 0`,
    # i.e. "complete". Their standing queries went in the same run (`subscriptions`), leaving copies
    # of those queries in a mailbox nothing would ever open. That is the false green this module's
    # own docstring calls worse than no safety net, and it is why the mailbox is matched by exact
    # equality against the channel the writer mints, per actor spelling, rather than by a pattern.
    (
        "session_events",
        "DELETE FROM session_events"
        f" WHERE session_id IN ({_SESSION_SCOPED})"
        " OR session_id = ANY(%(digest_channels)s)",
    ),
    # `holder` as well as the session scope: a turn lease names the actor holding it, and releasing
    # a departed person's lease is the point. The session-scoped half alone would leave a lease held
    # on a session whose ownership row had already gone in the same run.
    (
        "session_turns",
        "DELETE FROM session_turns "
        f"WHERE holder = ANY(%(actors)s) OR session_id IN ({_SESSION_SCOPED})",
    ),
    ("subscriptions", "DELETE FROM subscriptions WHERE owner = ANY(%(actors)s)"),
    ("user_preferences", "DELETE FROM user_preferences WHERE owner = ANY(%(actors)s)"),
    ("session_owners", "DELETE FROM session_owners WHERE owner = ANY(%(actors)s)"),
)

# The retained tier: counted, named, never deleted. Each entry is (table, actor columns, why it
# stays).
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
        "the record of every tool call this person's turns made — which is the only place some "
        "actions are recorded at all, and the credential writing it has no DELETE either",
    ),
    ("plan_approvals", ("actor",), "who approved a plan before it was allowed to spend anything"),
    (
        "note_proposals",
        ("actor", "decided_by"),
        "who proposed a knowledge note, and who signed it off at the PR-gate — the second is the "
        "human half of 'the agent proposes, a human decides' and is the whole reason the gate is "
        "auditable",
    ),
    ("bo_suggestions", ("actor",), "who a campaign's recommendation was made for"),
    ("bo_campaigns", ("opened_by",), "who framed an optimization campaign's decision space"),
    ("job_records", ("requested_by",), "who requested a durable calculation"),
    ("turn_costs", ("actor",), "what a person's turns cost, the record an operator bills against"),
)


# The retained tier again, for the one table whose person is **inside a payload**. Split out rather
# than folded above because the difference is exactly what hid it: `_RETAINED`'s entries are column
# names, `tests/test_leaver.py` derives its completeness check from column names, and this actor
# lives in a `jsonb` column called `document`. So the table was in neither tier, the CLI's two-tier
# report did not mention it, and the check that exists to catch precisely this could not see it —
# the `tool_result_links` failure the comment above describes, one indirection further out.
#
# **Retained rather than erased, by the same line as everything in `_RETAINED`.** A publication row
# is the outbox receipt for a result this system computed and handed to a database it does not own
# (`src/chemclaw/publish/`), and `Publication` records who asked for it and why. That is "who did
# what to the science", so it stays — and the report now says so with a number instead of by
# omission. What the number cannot cover is the destination: nothing here can reach a store this
# system does not own, and `schema/result-store/001_core.sql` gives that store its own `actor`
# column. An operator erasing a person has a second conversation to have, and the report is where
# they should find that out.
_RETAINED_IN_PAYLOAD: tuple[tuple[str, str, str, str], ...] = (
    (
        "result_publications",
        "document",
        "EXISTS (SELECT 1 FROM jsonb_array_elements(document -> 'publications') p"
        " WHERE p ->> 'actor' = ANY(%(actors)s))",
        "who asked for a result to be published and why — the receipt for a record that now also "
        "lives in a results store this system does not own, and cannot erase from",
    ),
)

# Tables that name a person and that **this command cannot reach at all**, with the reason. Neither
# tier applies: erasing is impossible and counting is impossible, so claiming either would be the
# false green again in the other direction.
#
# One entry, and it is a real one rather than a placeholder. `audit_anchors.reseal_by` records "who
# accepted the gap and why" (`infra/sql/032_audit_anchors.sql`); the code that wrote the table went
# with the audit hash chain in `D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-
# regulator-asks`, and `infra/sql/grants/app_privileges.sql` deliberately grants the runtime role
# nothing on it — so a `SELECT count(*)` from here would fail the whole erasure on
# `InsufficientPrivilege`. The schema is forward-only, so a deployment that ran the pre-removal
# build may still hold rows naming that operator, and an erasure that silently ignored them would
# be reporting a completeness it has not got.
_BEYOND_REACH: dict[str, str] = {
    "audit_anchors": "the runtime role holds no privilege on it and its writer was removed with "
    "the audit hash chain; rows from an older build need an operator with owner rights",
}


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
        actor: The Entra `oid` (or the configured dev actor id) whose data to erase. Matched by
            **exact equality against the two spellings this database holds of one id** — the bare
            id and the same id marked `unverified:` by a writer that could not authenticate its
            caller (`_actor_forms` has the whole argument). No pattern, no wildcard, no substring:
            the blast radius of getting one wrong is unrecoverable, so a person whose id merely
            *contains* this one cannot match. Either spelling may be given; they name one person.
        apply: Commit the deletion. The default counts and rolls back.

    Returns:
        The per-table counts for both tiers.

    Raises:
        ErasureError: `actor` is blank, or is the bare `unverified:` marker with no id behind it —
            either would otherwise match every row whose owner column is empty, and those are the
            un-attributed rows of a dev deployment, not one person's — or the database refused a
            statement (see below).
    """
    # `actors[0]` is the bare id: blank there means the caller named nobody — `""`, whitespace, or a
    # bare `unverified:` with no id behind it. Checked on that form rather than on all of them,
    # because the marked spelling of a blank id is the non-blank string `"unverified:"` and would
    # sail through, matching every marked row in the database.
    actors = _actor_forms(actor)
    if not actors[0].strip():
        raise ErasureError("actor must be a non-empty id; refusing to erase on a blank actor")

    report = ErasureReport(actor=actor, applied=apply)
    try:
        # `_session_dsn()`, not `postgres_dsn`: every table this sweep targets lives in the
        # session store, which a deployment may point elsewhere (`CHEMCLAW_SESSION_STORE_DSN`,
        # D-042) — the owner store, the transcript, the turn claims and, since the rebuild, the
        # checkpointer's tables (`agent/checkpointer.py` opens its pool on the same DSN). Erasing
        # against the default while the data lives on the configured one deletes nothing and
        # reports success, which is the one outcome a right-to-erasure sweep must never produce.
        # One digest per spelling of the id, for the reason `actors` is a list of spellings: a
        # memory written on the `unverified:` path is under a different namespace from one written
        # on the authenticated path, and both are the same chemist's.
        memory_prefixes = [memory_prefix(form) for form in actors]
        # The mailbox ids, one per spelling, minted by the function the writer and the reader both
        # use rather than re-spelled here — a second spelling of this string is a mailbox somebody
        # writes to and nobody erases, which is the defect this line closes.
        digest_channels = [digest_channel(form) for form in actors]
        async with db.connection(_session_dsn()) as conn:
            async with conn.cursor() as cur:
                for table, columns, _ in _RETAINED:
                    # One row counts once however many of its columns name this actor, in whichever
                    # spelling: a proposal they both wrote and reviewed is one retained record, not
                    # two, and a campaign whose `opened_by` is their id marked `unverified:` is
                    # still a record that names them.
                    predicate = " OR ".join(f"{column} = ANY(%(actors)s)" for column in columns)
                    await cur.execute(
                        f"SELECT count(*) FROM {table} WHERE {predicate}", {"actors": actors}
                    )
                    row = await cur.fetchone()
                    report.retained[table] = int(row[0]) if row else 0
                # The same tier, counted through a payload predicate rather than a column one —
                # one loop each because the predicate is the only thing that differs, and folding
                # them would put a `jsonb` path in the register a schema-derived test reads as
                # column names.
                for table, _column, predicate, _ in _RETAINED_IN_PAYLOAD:
                    await cur.execute(
                        f"SELECT count(*) FROM {table} WHERE {predicate}", {"actors": actors}
                    )
                    row = await cur.fetchone()
                    report.retained[table] = int(row[0]) if row else 0
                # Every table in `_ERASE` is asked about, not just the checkpointer's, so the
                # answer does not depend on remembering which ones might be missing.
                present = await existing_tables(cur, {table for table, _ in _ERASE})
                for table, statement in _ERASE:
                    if table not in present:
                        # Reported as zero rather than omitted: "this deployment holds none of your
                        # rows there" is true, and a report whose keys vary by deployment is one an
                        # operator cannot compare against another run.
                        report.erased[table] = 0
                        continue
                    # `memory_prefixes` alongside `actors` because the two erasure keys are
                    # genuinely different shapes: everything session-scoped matches an actor id,
                    # and the store matches the digest of one. Both are passed to every statement
                    # — psycopg ignores a parameter a statement does not name — so the loop stays
                    # one loop rather than branching on which key a table happens to use.
                    await cur.execute(
                        statement,
                        {
                            "actors": actors,
                            "memory_prefixes": memory_prefixes,
                            "digest_channels": digest_channels,
                        },
                    )
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
    """(table, why it is retained) for every retained table, so a report can print the reason.

    Exposed rather than inlined into the CLI's formatter because the reason is the substantive part
    of the answer: an operator asking "why is this row still here?" should read it from the module
    that decided, not from a print statement.
    """
    return tuple((table, reason) for table, _, reason in _RETAINED) + tuple(
        (table, reason) for table, _column, _predicate, reason in _RETAINED_IN_PAYLOAD
    )


def unreachable_tables() -> tuple[tuple[str, str], ...]:
    """(table, why) for every table this erasure can neither clear nor count.

    Reported rather than omitted, for the reason the retained tier is reported at all: an operator
    signing off on an erasure needs to know which questions this command did not answer. Omitting
    them is what turns a partial erasure into one that looks complete.
    """
    return tuple(_BEYOND_REACH.items())
