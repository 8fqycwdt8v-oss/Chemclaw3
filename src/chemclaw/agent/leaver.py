"""Erase a departed person's conversational data, and keep the record of what they did.

Removing an Entra role stops access but deletes nothing; this answers "remove their data". The line
is drawn at attribution, in two tiers:

- **Erasable — the conversation** (`_ERASE`): sessions, messages, events, leases, preferences,
  memories, personal skills, subscriptions. How one person worked; not evidence about chemistry.
- **Retained — the record** (`_RETAINED`, `_RETAINED_IN_PAYLOAD`): audit trail, approvals,
  proposals, BO suggestions, jobs, costs, effects, campaigns, protocols. Each says who did what to
  the science, and an attributable record that can be deleted on request is not attributable.

The command reports both tiers with row counts, plus what it cannot reach (`_BEYOND_REACH`), so a
partial erasure never looks complete. Erasure is irreversible and identity-scoped: exact equality
against the spellings of one id (`_actor_forms`), never a pattern. The default is a dry run.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field

import psycopg

from chemclaw.agent.checkpointer import CHECKPOINT_TABLES, checkpoint_thread_delete_statements
from chemclaw.agent.local_skills import local_skills_prefix
from chemclaw.agent.scratchpad import memory_prefix
from chemclaw.agent.session_store import (
    SessionTurnClaims,
    _session_delete_statements,
    _session_dsn,
)
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.db import existing_tables
from chemclaw.core.errors import ChemclawError
from chemclaw.durable.digest import digest_channel

logger = logging.getLogger(__name__)


class ErasureError(ChemclawError):
    """An erasure cannot proceed: a blank actor, or a statement the database refused.

    A `ChemclawError` (a `ValueError`), so entry points catch it without importing the database
    driver.
    """


# The prefix a writer stamps on an actor it could not authenticate. Duplicated from
# `connectors/bo/server/tools.py` because core must not import a bundle; a third writer should move
# it to a shared home.
_UNVERIFIED_ACTOR_PREFIX = "unverified:"


def _actor_forms(actor: str) -> list[str]:
    """Every spelling this database legitimately holds of *one* person's id.

    A writer that cannot authenticate its caller records the claimed id as `unverified:<id>` (the BO
    bundle's synchronous MCP path), while authenticated paths write the bare id; both are the same
    person. Matched by exact equality against this closed set (`= ANY(...)`), never `LIKE`, which
    would match `oid-erik-2` when erasing `oid-erik`. Either spelling may be given; the marker is
    stripped first so the set stays at two. Applied to both tiers, since this is a property of the
    id.
    """
    base = actor.removeprefix(_UNVERIFIED_ACTOR_PREFIX)
    return [base, f"{_UNVERIFIED_ACTOR_PREFIX}{base}"]


# The conversational tier, deleted in dependency order: rows keyed by `session_id` before the
# `session_owners` rows that are the only way to find them. (table, SQL) pairs so the report can
# attribute counts. `= ANY(%(actors)s)` is exact equality against each spelling from `_actor_forms`.
_SESSION_SCOPED = "SELECT session_id FROM session_owners WHERE owner = ANY(%(actors)s)"
# The sessions somebody else owns that this person was let into — claimed for the sweep's duration,
# never residue-probed (`_actor_sessions`).
_MEMBER_OF = "SELECT session_id FROM session_members WHERE actor = ANY(%(actors)s)"
# The LangGraph checkpointer holds the same conversation as graph state keyed by session id, so it
# is erased in the same pass, before `session_owners`. These tables are created by
# `AsyncPostgresSaver.setup()`, not a migration, so they are skipped when absent; the check is a
# separate query (`core.db.existing_tables`) because Postgres resolves a missing table at parse
# time. Statement order comes from `checkpoint_thread_delete_statements`.
_CHECKPOINT_ERASE: tuple[tuple[str, str], ...] = checkpoint_thread_delete_statements(
    f"thread_id IN ({_SESSION_SCOPED})"
)


# The agent's durable memories and the chemist's own skills are not session-scoped; the store's
# namespace is keyed by actor digest, which is what makes them findable here. `store_vectors` goes
# before `store` (foreign key). Skipped when absent, since `AsyncPostgresStore.setup()` creates
# these tables.
def store_prefixes(actors: Sequence[str]) -> list[str]:
    """Every `store.prefix` a departing person's rows live under, across both tiers.

    Memories and personal skills share the `store` table under different namespaces. Built from the
    writers' own prefix functions, one per spelling of the id.

    Args:
        actors: Every spelling of the departing person's id.

    Returns:
        The prefixes `_MEMORY_ERASE`'s statements delete by.
    """
    return [memory_prefix(form) for form in actors] + [local_skills_prefix(form) for form in actors]


_MEMORY_ERASE: tuple[tuple[str, str], ...] = (
    ("store_vectors", "DELETE FROM store_vectors WHERE prefix = ANY(%(memory_prefixes)s)"),
    ("store", "DELETE FROM store WHERE prefix = ANY(%(memory_prefixes)s)"),
)
_ERASE: tuple[tuple[str, str], ...] = (
    # The full text of everything this person's tools returned, found through session-scoped links.
    # The DELETE targets the blob and the link follows by cascade, because the runtime role
    # deliberately holds no DELETE on `tool_result_links`.
    (
        "tool_result_blobs",
        # Only blobs nobody else can still read: a blob is spared while some link belongs to a
        # session whose owner is not the leaver (an unowned session, `o.owner IS NULL`, still
        # spares). An orphan link, whose session cannot be reopened, spares nothing. Mirrors
        # `session_store._SESSION_DELETE`.
        "DELETE FROM tool_result_blobs b WHERE EXISTS ("
        "  SELECT 1 FROM tool_result_links l"
        f"   WHERE l.content_hash = b.content_hash AND l.session_id IN ({_SESSION_SCOPED})"
        ") AND NOT EXISTS ("
        "  SELECT 1 FROM tool_result_links l"
        "    JOIN session_owners o ON o.session_id = l.session_id"
        "   WHERE l.content_hash = b.content_hash"
        "     AND (o.owner IS NULL OR o.owner <> ALL(%(actors)s)))",
    ),
    # By session and by author, so a member's words in a session somebody else owns are erased too.
    # The same words in the owner's checkpointed thread are beyond reach (`_BEYOND_REACH`).
    (
        "session_messages",
        "DELETE FROM session_messages "
        f"WHERE session_id IN ({_SESSION_SCOPED}) OR actor = ANY(%(actors)s)",
    ),
    # Artefacts are the conversation's working documents. Deleting the header cascades its revisions
    # (the revision table is INSERT-only by grant). By session only; see `_BEYOND_REACH`.
    (
        "session_exhibits",
        f"DELETE FROM session_exhibits WHERE session_id IN ({_SESSION_SCOPED})",
    ),
    # Uploads by session and by uploader: everything in the leaver's sessions, and their files in
    # sessions somebody else owns.
    (
        "session_attachments",
        "DELETE FROM session_attachments "
        f"WHERE session_id IN ({_SESSION_SCOPED}) OR uploaded_by = ANY(%(actors)s)",
    ),
    *_CHECKPOINT_ERASE,
    *_MEMORY_ERASE,
    # Digests land in the synthetic mailbox `digest-<oid>`, which has no `session_owners` row, so it
    # is matched by exact equality against the channel id the writer mints, per spelling.
    (
        "session_events",
        "DELETE FROM session_events"
        f" WHERE session_id IN ({_SESSION_SCOPED})"
        " OR session_id = ANY(%(digest_channels)s)",
    ),
    # `holder` and `actor` as well as the session scope: a lease names the actor holding it and the
    # sender of the turn it covers.
    (
        "session_turns",
        "DELETE FROM session_turns "
        "WHERE holder = ANY(%(actors)s) OR actor = ANY(%(actors)s) "
        f"OR session_id IN ({_SESSION_SCOPED})",
    ),
    ("subscriptions", "DELETE FROM subscriptions WHERE owner = ANY(%(actors)s)"),
    ("user_preferences", "DELETE FROM user_preferences WHERE owner = ANY(%(actors)s)"),
    # Erasable: a rate-limiting counter, not a record of the science. `turn_costs` stays retained as
    # the spend record.
    ("budget_usage", "DELETE FROM budget_usage WHERE actor = ANY(%(actors)s)"),
    # A composed workflow is the person's own working procedure, reachable only by its owner, so it
    # goes with the conversation. `approved_by` can only ever hold the owner, so the `owner`
    # predicate covers it.
    ("composed_workflows", "DELETE FROM composed_workflows WHERE owner = ANY(%(actors)s)"),
    # Shared-session membership and plan authorship are the person's standing in conversations, not
    # records of the science. By person and by session, so the report counts both.
    (
        "session_members",
        "DELETE FROM session_members "
        f"WHERE actor = ANY(%(actors)s) OR session_id IN ({_SESSION_SCOPED})",
    ),
    (
        "plan_authors",
        "DELETE FROM plan_authors "
        f"WHERE actor = ANY(%(actors)s) OR session_id IN ({_SESSION_SCOPED})",
    ),
    # A queued message waiting in a session's line; a leaver's must not run after they are gone.
    (
        "session_turn_queue",
        "DELETE FROM session_turn_queue "
        f"WHERE sender = ANY(%(actors)s) OR session_id IN ({_SESSION_SCOPED})",
    ),
    # A cross-replica request to a running turn names its asker; relayed frames go by cascade.
    # `holder` names a process, not a person.
    (
        "session_turn_remotes",
        "DELETE FROM session_turn_remotes "
        f"WHERE actor = ANY(%(actors)s) OR session_id IN ({_SESSION_SCOPED})",
    ),
    ("session_owners", "DELETE FROM session_owners WHERE owner = ANY(%(actors)s)"),
)

# The retained tier: counted, named, never deleted. Each entry is (table, actor columns, why it
# stays). Several columns per table, because one row can name a person twice (`note_proposals.actor`
# and `decided_by`); `tests/test_leaver.py` derives the set from the live schema.
_RETAINED: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "audit_events",
        ("actor",),
        "the record of every tool call this person's turns made — which is the only place some "
        "actions are recorded at all, and the credential writing it has no DELETE either",
    ),
    ("plan_approvals", ("actor",), "who approved a plan before it was allowed to spend anything"),
    (
        "behaviour_proposals",
        ("actor", "decided_by"),
        "who proposed a change to what the agent does, and who decided it — the row above's "
        "reason one layer up, since this is about the agent's behaviour rather than one plan's "
        "spend. **It retains more than that row and the report says so rather than letting it "
        "ride**: a plan approval is a hash and a verdict, while a proposal keeps `content` — a "
        "whole document a model wrote about this person's chemistry, held after they leave. That "
        "is justified (a rejection is only evidence if the text it rejected is still there) and "
        "it is a larger claim, which is exactly why it is printed with a count rather than "
        "assumed",
    ),
    (
        "note_proposals",
        ("actor", "decided_by"),
        "who proposed a knowledge note, and who signed it off, while there was a PR-gate to sign "
        "one off at (`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` retired it). No row "
        "is written here any more and the table stays for the rows that are: a deployment that ran "
        "the gate holds real sign-offs by real people, and an erasure request must still find them",
    ),
    ("bo_suggestions", ("actor",), "who a campaign's recommendation was made for"),
    ("bo_campaigns", ("opened_by",), "who framed an optimization campaign's decision space"),
    ("job_records", ("requested_by",), "who requested a durable calculation"),
    (
        "effects",
        ("requested_by", "approved_by"),
        "who asked this system to change something in a system it does not own, and who "
        "approved it when the change could not be undone — the strongest case in this tier, "
        "because the change is still standing on the far side and somebody there may need "
        "to know on whose authority it was made",
    ),
    (
        "pending_requests",
        ("requested_by", "answered_by"),
        "who asked somebody else to run, review or deliver something, and who answered — the "
        "standing a plan approval has, and for the same reason: an answer that released a "
        "durable workflow is only as good as the ability to say later who gave it. `asked_of` "
        "is deliberately not here — it is advisory routing rather than an act, and it may hold "
        "an entitlement rather than a person",
    ),
    (
        "pending_request_answers",
        ("requested_by", "answered_by"),
        "the same answer, archived when the question was asked again — `pending_requests`' row one "
        "hop later, so scrubbed on the same columns and for the same reason. `asked_of` stays here "
        "for the reason it stays there",
    ),
    ("turn_costs", ("actor",), "what a person's turns cost, the record an operator bills against"),
    # The prescriptive-tier columns: who framed a design is part of its provenance. A revision's
    # `author` (with `author_kind`) distinguishes an agent's draft from an expert's correction,
    # which is why that table is append-only and its rows are never erased.
    (
        "experiment_protocols",
        ("opened_by",),
        "who opened a prescriptive experiment design — the provenance of an artifact a laboratory "
        "may still act on",
    ),
    (
        "experiment_protocol_revisions",
        ("author",),
        "who wrote each revision of a design — with `author_kind`, the thing that makes an "
        "expert's correction of a generated protocol attributable at all",
    ),
    (
        "experiment_arm_results",
        ("author",),
        "who attached a measured outcome to a designed arm — the provenance of a number a "
        "laboratory acts on, and with `author_kind` the thing that says whether a person or this "
        "system put it there",
    ),
    (
        "experiment_protocol_status_events",
        ("actor",),
        "who approved, ran or abandoned a design and at which revision — the strongest case of "
        "the three, because an approval with nobody attached to it is not a smaller record of an "
        "approval, it is a claim that one happened",
    ),
)


# The retained tier for tables whose person is inside a `jsonb` payload, which a column-name check
# cannot see. Entries are `(table, the column the person is inside, the predicate that finds them,
# why the row stays)`; the column is listed so `tests/test_leaver.py` can assert it exists.
#
# A publication row is the outbox receipt for a result handed to an external store, so it stays.
# That store has its own `actor` column, which this command cannot reach.
_RETAINED_IN_PAYLOAD: tuple[tuple[str, str, str, str], ...] = (
    (
        "result_publications",
        "document",
        # The `jsonb_typeof` guard keeps this a count: `jsonb_array_elements` raises on a non-array,
        # and one malformed row would otherwise abort every erasure. An unreadable row counts 0.
        "jsonb_typeof(document -> 'publications') = 'array'"
        " AND EXISTS (SELECT 1 FROM jsonb_array_elements(document -> 'publications') p"
        " WHERE p ->> 'actor' = ANY(%(actors)s))",
        "who asked for a result to be published and why — the receipt for a record that now also "
        "lives in a results store this system does not own, and cannot erase from",
    ),
)

# Places that name a person and that this command can neither erase nor count, with the reason.
# Reported so an erasure does not claim a completeness it lacks. `audit_anchors` is one: the runtime
# role holds no privilege on it.
_BEYOND_REACH: dict[str, str] = {
    "commitments": (
        "`owner` is a name in the *portfolio system's* namespace, not an Entra oid, and this "
        "deployment holds no mapping between the two — one it invented would be a second "
        "directory, "
        "and a wrong entry would erase somebody else's work while reporting a success. The row is "
        "also not this system's to delete: it is a mirror, and the next sync would restore it. "
        "Erasure belongs to the system that owns the export, and this command says so rather than "
        "matching on a string that may not be the person"
    ),
    "audit_anchors": "the runtime role holds no privilege on it and its writer was removed with "
    "the audit hash chain, so this deployment's copy is empty; a schema is forward-only, so a "
    "database that ran the pre-removal build needs an operator with owner rights to check",
    # Entries below that are not tables name places a person's id is written that no query can reach
    # (a note's `actor:` frontmatter in git; the checkpointed thread of a session somebody else
    # owns).
    "session_exhibit_revisions": "an artefact's revisions go with their header, which the erase "
    "tier deletes for every session this person owns (`session_exhibits`). What is left is a "
    "member's revision of an artefact in a session somebody *else* owns: a revision cannot be cut "
    "out of the middle of an append-only history without rewriting the owner's document under "
    "them, and the runtime role holds no DELETE on the table by design. It goes when the owner "
    "deletes the session or leaves. Find those before erasing with `SELECT DISTINCT e.session_id "
    "FROM session_exhibit_revisions r JOIN session_exhibits e USING (exhibit_id) WHERE r.author = "
    "'<id>'`",
    "session_exhibits (`created_by`, `head_author` in a session somebody else owns)": "the erase "
    "tier deletes every artefact in a session this person owns. A member who created or last "
    "revised an artefact in somebody else's session is named on that artefact's header, and the "
    "header is the owner's document: blanking its author would misattribute the revisions under "
    "it, and deleting it would delete the owner's work. It goes when the owner deletes the "
    "session or leaves. Find those before erasing with `SELECT session_id, exhibit_id FROM "
    "session_exhibits WHERE created_by = '<id>' OR head_author = '<id>'`",
    "a shared session's graph state (checkpoints of sessions this person was a member of)": "a "
    "member's words are erased from the transcript by author, but the owner's checkpointed thread "
    "still carries them — it is the owner's conversation state and is removed when the owner "
    "deletes the session or leaves. Find those sessions before erasing with "
    "`SELECT session_id FROM session_members WHERE actor = '<id>'`",
    "knowledge notes (`actor:` frontmatter)": "an agent-written note names the person it was "
    "written for, and the note lives in the knowledge repository rather than in this database, so "
    "this command neither counts nor clears it. Find them with `git grep -l 'actor: <id>'` in the "
    "note repository. The note is the record and stays, as the audit trail does; removing the name "
    "from it is a rewrite of that repository's history, which is its owner's decision",
}


# How many sessions one claim, refresh or release statement covers. A constant, not a setting: it
# bounds one statement's lock window and how much is re-claimed on failure.
CLAIM_BATCH = 1000

# Claim refreshes per lease, matching `api/state._CLAIM_REFRESHES_PER_LEASE` (not importable:
# `agent/` sits below `api/`).
_CLAIM_REFRESHES_PER_LEASE = 3


# The holder name for this sweep's turn claims: fresh per run so a later erasure cannot refresh or
# release them, and prefixed so an operator can recognise an erasure in `session_turns.holder`.
def _erasure_holder() -> str:
    """This run's identity as a turn-claim holder."""
    return f"erase:{uuid.uuid4().hex}"


# Which column names the session, per table, for the residue count; derived from
# `session_store._session_delete_statements()`. Blobs carry no session, so their links are counted.
_RESIDUE_LINK_TABLE = "tool_result_links"


def _residue_columns() -> tuple[tuple[str, str], ...]:
    """`(table, the column that names the session)` for every table a residue could land in."""
    return tuple(
        (table, "thread_id" if table in CHECKPOINT_TABLES else "session_id")
        for table, _ in _session_delete_statements()
        if table != "tool_result_blobs"
    ) + ((_RESIDUE_LINK_TABLE, "session_id"),)


@dataclass
class ErasureReport:
    """What was removed, what was deliberately kept, and whether anything was actually written."""

    actor: str
    applied: bool
    erased: dict[str, int] = field(default_factory=dict)
    retained: dict[str, int] = field(default_factory=dict)
    residue: dict[str, int] = field(default_factory=dict)
    # The session ids the residue is under, so `finish_erasure` can delete by id after
    # `session_owners` is gone.
    residue_sessions: list[str] = field(default_factory=list)

    @property
    def erased_total(self) -> int:
        """How many conversational rows this run removed (or would remove, in a dry run)."""
        return sum(self.erased.values())

    @property
    def retained_total(self) -> int:
        """How many rows carry this actor and stay, because the record needs them."""
        return sum(self.retained.values())

    @property
    def residue_total(self) -> int:
        """Rows that reappeared under a session id nothing can reach again — zero on a clean run.

        Non-zero means re-running `erase_actor` cannot finish the job, because session-scoped sweeps
        find sessions through `session_owners`; `finish_erasure` deletes by the ids in
        `residue_sessions`.
        """
        return sum(self.residue.values())


async def _actor_sessions(actors: list[str]) -> tuple[list[str], list[str]]:
    """The sessions this erasure reaches: `(owned, shared)`, read before the sweep opens.

    On its own connection, because the claims are taken before the erasure transaction and the
    residue count runs after `session_owners` is gone. `shared` (sessions the person is a member of)
    are claimed too, since their messages there are deleted by author, but are not residue-probed.
    """
    async with db.connection(_session_dsn()) as conn:
        async with conn.cursor() as cur:
            await cur.execute(_SESSION_SCOPED, {"actors": actors})
            owned = [str(row[0]) for row in await cur.fetchall()]
            await cur.execute(_MEMBER_OF, {"actors": actors})
            shared = [str(row[0]) for row in await cur.fetchall() if str(row[0]) not in owned]
    return owned, shared


async def _residue_for(sessions: list[str]) -> tuple[dict[str, int], list[str]]:
    """Count what still names these sessions, and say which ones — after their owner rows are gone.

    Covers what the turn claims cannot: sessions created mid-sweep, lapsed leases, and deployments
    with no durable claims at all.
    """
    probes = _residue_columns()
    residue: dict[str, int] = {}
    holding: set[str] = set()
    async with db.connection(_session_dsn()) as conn:
        async with conn.cursor() as cur:
            present = await existing_tables(cur, {table for table, _ in probes})
            for table, column in probes:
                if table not in present:
                    continue
                # Grouped by session, because `finish_erasure` needs the ids.
                await cur.execute(
                    f"SELECT {column}, count(*) FROM {table} "
                    f"WHERE {column} = ANY(%(sessions)s) GROUP BY {column}",
                    {"sessions": sessions},
                )
                for session_id, count in await cur.fetchall():
                    if int(count):
                        residue[table] = residue.get(table, 0) + int(count)
                        holding.add(str(session_id))
    return residue, sorted(holding)


async def erase_actor(actor: str, *, apply: bool = False) -> ErasureReport:
    """Count — and, with `apply`, delete — one actor's conversational rows.

    One transaction, so a dry run (real DELETEs, rolled back) shows exactly what an apply would do
    and a failure leaves nothing half-erased. A turn running on one of the person's sessions refuses
    the whole run, dry or applied (`_sessions_held`). After an applied run every reached session is
    re-counted; anything that came back is reported on `ErasureReport.residue` and logged at ERROR.

    Args:
        actor: The Entra `oid` (or dev actor id) to erase, matched by exact equality against its two
            spellings (`_actor_forms`); either spelling may be given.
        apply: Commit the deletion. The default counts and rolls back.

    Returns:
        The per-table counts for both tiers, and `residue` — empty on every clean run.

    Raises:
        ErasureError: `actor` is blank (or a bare `unverified:`), a turn is running on one of this
            person's sessions, or the database refused a statement.
    """
    # Blank is checked on the bare form, since the marked spelling of a blank id is the non-blank
    # `"unverified:"` and would match every marked row.
    actors = _actor_forms(actor)
    if not actors[0].strip():
        raise ErasureError("actor must be a non-empty id; refusing to erase on a blank actor")

    report = ErasureReport(actor=actor, applied=apply)
    try:
        sessions, shared = await _actor_sessions(actors)
    except psycopg.Error as exc:
        raise ErasureError(f"the database refused the erasure: {exc}") from exc
    async with _sessions_held(sessions + shared):
        await _erase_within_claims(actors, report, apply=apply)
    if apply and sessions:
        try:
            report.residue, report.residue_sessions = await _residue_for(sessions)
        except psycopg.Error as exc:
            raise ErasureError(f"the database refused the erasure: {exc}") from exc
        if report.residue:
            # ERROR rather than raise: the deletion happened and its counts are real; the log and
            # `residue` stop it being read as complete.
            logger.error(
                "erasure for actor %s is INCOMPLETE: %s row(s) came back under session id(s) "
                "whose ownership row is gone, so no *actor*-scoped erasure can reach them: %s. "
                "A turn was running on one of these sessions while the sweep ran; stop it, then "
                "finish the "
                "erasure with `python -m chemclaw.cli.erase_actor --finish %s --apply`.",
                actor,
                report.residue_total,
                ", ".join(f"{table}={count}" for table, count in sorted(report.residue.items())),
                " ".join(report.residue_sessions),
            )
    logger.info(
        "erasure %s for actor: %d conversational row(s) across %d table(s); "
        "%d attributed row(s) retained",
        "applied" if apply else "previewed",
        report.erased_total,
        len([t for t, n in report.erased.items() if n]),
        report.retained_total,
    )
    return report


@asynccontextmanager
async def _sessions_held(sessions: list[str]) -> AsyncIterator[None]:
    """Hold the durable turn claim on every session about to be erased, or refuse the whole run.

    Without it, a live turn can rewrite its in-memory messages onto the thread after the sweep,
    under a session id no later erasure can reach. Only the durable claim crosses processes. Refuse
    rather than wait, so the operator can act on a named session. Claims taken before a refusal are
    released; release is identity-checked by the per-run holder. Claims are taken in `CLAIM_BATCH`
    batches and kept alive by `_keep_claims_alive` for as long as the deletion runs.

    Raises:
        ErasureError: a turn holds one of these sessions.
    """
    if not sessions:
        yield
        return
    claims = SessionTurnClaims()
    holder = _erasure_holder()
    lease = settings.service_turn_claim_lease_seconds
    held: list[str] = []
    heartbeat: asyncio.Task[None] | None = None
    try:
        busy: list[str] = []
        for batch in _batches(sessions):
            try:
                taken = await claims.claim_many(batch, holder, lease)
            except psycopg.Error as exc:
                raise ErasureError(f"the database refused the erasure: {exc}") from exc
            held.extend(session_id for session_id in batch if session_id in taken)
            busy.extend(session_id for session_id in batch if session_id not in taken)
        if busy:
            raise ErasureError(
                "a turn is running on "
                + ", ".join(sorted(busy))
                + "; stop it (POST /sessions/{id}/turn/stop) and run the erasure again. "
                "Erasing a session while a turn writes to it leaves a copy of the conversation "
                "that no later erasure can reach."
            )
        heartbeat = asyncio.create_task(_keep_claims_alive(claims, list(held), holder, lease))
        yield
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        for batch in _batches(held):
            try:
                await claims.release_many(batch, holder)
            except psycopg.Error:
                # The lease is the backstop: an unreleased claim costs one lease of unavailability.
                logger.warning(
                    "could not release the erasure's turn claim on %d session(s) (%s ...); they "
                    "expire on their own after %ss",
                    len(batch),
                    batch[0],
                    lease,
                    exc_info=True,
                )


def _batches(sessions: list[str]) -> list[list[str]]:
    """`sessions` in `CLAIM_BATCH`-sized batches, each free of repeats.

    `ON CONFLICT … DO UPDATE` cannot touch one row twice in a statement, so repeats would raise
    `CardinalityViolation`.
    """
    unique = list(dict.fromkeys(sessions))
    return [unique[start : start + CLAIM_BATCH] for start in range(0, len(unique), CLAIM_BATCH)]


async def _keep_claims_alive(
    claims: SessionTurnClaims, sessions: list[str], holder: str, lease: float
) -> None:
    """Push this sweep's claims out for as long as the sweep runs, and say what it loses.

    A sweep can outlast the claim lease, after which another pod could take a session. This is the
    same heartbeat a running turn keeps (`api/state._hold_turn_claim`). A failed refresh is normal
    at the end (the erase deletes these rows); only a session another holder now names is warned
    about and dropped from the heartbeat. Cancelled by `_sessions_held`'s `finally`.
    """
    interval = lease / _CLAIM_REFRESHES_PER_LEASE
    alive = list(sessions)
    while alive:
        await asyncio.sleep(interval)
        try:
            refreshed: set[str] = set()
            for batch in _batches(alive):
                refreshed |= await claims.refresh_many(batch, holder, lease)
            missing = [session_id for session_id in alive if session_id not in refreshed]
            taken_over: set[str] = set()
            for batch in _batches(missing):
                taken_over |= await claims.other_holders(batch, holder)
            if taken_over:
                logger.warning(
                    "the erasure's turn claim on %d session(s) was taken over while the sweep was "
                    "running (%s); a turn may be writing to a session this run is erasing, so "
                    "check the residue this run reports",
                    len(taken_over),
                    ", ".join(sorted(taken_over)[:10]),
                )
                alive = [session_id for session_id in alive if session_id not in taken_over]
        except psycopg.Error:
            # Warned rather than fatal: ending mid-erasure would be worse; the residue count says
            # whether the lost lease cost anything.
            logger.warning(
                "could not refresh the erasure's turn claims; if this keeps failing they lapse "
                "after %ss and a turn may start on a session this run is erasing",
                lease,
                exc_info=True,
            )


async def _erase_within_claims(actors: list[str], report: ErasureReport, *, apply: bool) -> None:
    """The sweep itself: count both tiers and delete the conversational one, in one transaction.

    Raises:
        ErasureError: the database refused a statement.
    """
    try:
        # `_session_dsn()`, not `postgres_dsn`: every table here lives in the session store, which
        # may be configured elsewhere; erasing against the wrong database would delete nothing and
        # report success. One store prefix per spelling of the id.
        memory_prefixes = store_prefixes(actors)
        # Mailbox ids minted by the writer's own function, one per spelling.
        digest_channels = [digest_channel(form) for form in actors]
        async with db.connection(_session_dsn()) as conn:
            async with conn.cursor() as cur:
                for table, columns, _ in _RETAINED:
                    # One row counts once however many of its columns name this actor, in either
                    # spelling.
                    predicate = " OR ".join(f"{column} = ANY(%(actors)s)" for column in columns)
                    await cur.execute(
                        f"SELECT count(*) FROM {table} WHERE {predicate}", {"actors": actors}
                    )
                    row = await cur.fetchone()
                    report.retained[table] = int(row[0]) if row else 0
                # The same tier, counted through payload predicates; kept separate so the column
                # register stays column names.
                for table, _column, predicate, _ in _RETAINED_IN_PAYLOAD:
                    await cur.execute(
                        f"SELECT count(*) FROM {table} WHERE {predicate}", {"actors": actors}
                    )
                    row = await cur.fetchone()
                    report.retained[table] = int(row[0]) if row else 0
                # Every table in `_ERASE` is asked about, not just the checkpointer's, so the answer
                # does not depend on remembering which ones might be missing.
                present = await existing_tables(cur, {table for table, _ in _ERASE})
                for table, statement in _ERASE:
                    if table not in present:
                        # Zero rather than omitted, so reports from different deployments have the
                        # same keys.
                        report.erased[table] = 0
                        continue
                    # Both keys (actor ids and store prefixes) are passed to every statement;
                    # psycopg ignores unnamed parameters.
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
        # Translated here: what reaches this is a statement refusal (usually `InsufficientPrivilege`
        # when grants were not re-applied), and `chemclaw.cli` may not import a database driver.
        raise ErasureError(f"the database refused the erasure: {exc}") from exc


# The one residue table `finish_erasure` cannot clear: the runtime role holds no DELETE on
# `tool_result_links` (a link disappears only behind its blob). Orphan links are collected by
# `durable/retention.py`. Named so the report does not show it as zero.
_FINISH_LEAVES: dict[str, str] = {
    _RESIDUE_LINK_TABLE: (
        "the runtime role is denied DELETE here on purpose — a link may only disappear behind the "
        "content-addressed blob it points at, and `durable/retention.py`'s "
        "`retention_tool_results_days` sweep is what collects both"
    ),
}


@dataclass
class ResidueReport:
    """What finishing an interrupted erasure removed, and what it could not.

    Separate from `ErasureReport`: this is keyed by orphaned session ids and has no retained tier,
    which is keyed by actor and unreachable from here.
    """

    sessions: list[str]
    applied: bool
    removed: dict[str, int] = field(default_factory=dict)
    remaining: dict[str, int] = field(default_factory=dict)
    refused: dict[str, str] = field(default_factory=dict)

    @property
    def removed_total(self) -> int:
        """How many rows this run removed, or would remove in a dry run."""
        return sum(self.removed.values())

    @property
    def finished(self) -> bool:
        """Whether every named session is now gone from every table this route can reach."""
        return not self.remaining and not self.refused


_STILL_OWNED = "SELECT session_id FROM session_owners WHERE session_id = ANY(%(sessions)s)"


async def finish_erasure(sessions: list[str], *, apply: bool = False) -> ResidueReport:
    """Delete an interrupted erasure's residue by session id, and prove it is gone.

    Runs `session_store._session_delete_statements()`, which never reads `session_owners`. A session
    that is still owned is refused, so this can only finish what is already beyond the actor erasure
    and is never an unscoped conversation delete. Takes the same durable turn claims as
    `erase_actor`, and its dry run is real (rolled back).

    Args:
        sessions: The session ids to clear — `ErasureReport.residue_sessions`, verbatim.
        apply: Commit. The default counts and rolls back.

    Returns:
        A `ResidueReport`: what went, what is still there afterwards, and any session refused
        because it is still owned.

    Raises:
        ErasureError: no session was named, a turn is running on one of them, or the database
            refused a statement.
    """
    named = [session_id for session_id in dict.fromkeys(sessions) if session_id.strip()]
    if not named:
        raise ErasureError("name at least one session id; refusing to finish an empty erasure")
    report = ResidueReport(sessions=named, applied=apply)
    try:
        async with db.connection(_session_dsn()) as conn:
            async with conn.cursor() as cur:
                await cur.execute(_STILL_OWNED, {"sessions": named})
                for row in await cur.fetchall():
                    report.refused[str(row[0])] = (
                        "still has an ownership row, so it is reachable by the actor erasure and "
                        "by its owner; this route only finishes what is already orphaned"
                    )
    except psycopg.Error as exc:
        raise ErasureError(f"the database refused the erasure: {exc}") from exc
    targets = [session_id for session_id in named if session_id not in report.refused]
    if not targets:
        return report
    async with _sessions_held(targets):
        await _delete_orphaned_sessions(targets, report, apply=apply)
    if apply:
        try:
            report.remaining, _ = await _residue_for(targets)
        except psycopg.Error as exc:
            raise ErasureError(f"the database refused the erasure: {exc}") from exc
        # The link table is expected to survive and says so in `_FINISH_LEAVES`; reporting it as
        # "remaining" would make every finish read as unfinished for ever.
        report.remaining.pop(_RESIDUE_LINK_TABLE, None)
    if report.remaining:
        logger.error(
            "finishing the erasure of session(s) %s left %d row(s) behind: %s. Another turn wrote "
            "to one of these sessions while this ran; stop it and run the same command again.",
            " ".join(targets),
            sum(report.remaining.values()),
            ", ".join(f"{table}={count}" for table, count in sorted(report.remaining.items())),
        )
    else:
        logger.info(
            "erasure %s for %d orphaned session(s): %d row(s) across %d table(s)",
            "finished" if apply else "previewed",
            len(targets),
            report.removed_total,
            len([t for t, n in report.removed.items() if n]),
        )
    return report


async def _delete_orphaned_sessions(
    sessions: list[str], report: ResidueReport, *, apply: bool
) -> None:
    """Run the per-session deletes for every named session, in one transaction.

    Raises:
        ErasureError: the database refused a statement.
    """
    statements = _session_delete_statements()
    try:
        async with db.connection(_session_dsn()) as conn:
            async with conn.cursor() as cur:
                present = await existing_tables(cur, {table for table, _ in statements})
                for table, statement in statements:
                    # Zero rather than absent, so reports have the same keys.
                    report.removed.setdefault(table, 0)
                    if table not in present:
                        continue
                    for session_id in sessions:
                        await cur.execute(statement, {"session_id": session_id})
                        report.removed[table] += max(cur.rowcount, 0)
            if apply:
                await conn.commit()
            else:
                await conn.rollback()
    except psycopg.Error as exc:
        raise ErasureError(f"the database refused the erasure: {exc}") from exc


def finish_leaves() -> tuple[tuple[str, str], ...]:
    """(table, why) for what a finished erasure still leaves behind, so the CLI can print it."""
    return tuple(_FINISH_LEAVES.items())


def retention_reasons() -> tuple[tuple[str, str], ...]:
    """(table, why it is retained) for every retained table, so a report can print the reason."""
    return tuple((table, reason) for table, _, reason in _RETAINED) + tuple(
        (table, reason) for table, _column, _predicate, reason in _RETAINED_IN_PAYLOAD
    )


def unreachable_tables() -> tuple[tuple[str, str], ...]:
    """(table, why) for every table this erasure can neither clear nor count.

    Reported so an operator knows which questions this command did not answer.
    """
    return tuple(_BEYOND_REACH.items())
