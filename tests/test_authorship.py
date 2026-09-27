"""One authorship model — a person and an agent — across notes, the audit trail and transcripts.

`D-2026-09-27-an-author-is-a-person-and-an-agent` decided the shape once (`core/authorship.py`):
`actor` is the human principal something was written for, `agent` the agent that wrote it, `None`
when a human wrote it directly and `""` when an agent did and goes unnamed. These tests hold each
subsystem to it from the entry point production calls — `record_note`, `save_messages`, the
transcript route's projection, `fork_session`, `erase_actor`, and migration 109's own statements —
rather than from the model, because a model every subsystem imports and none of them writes is the
shape `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` deleted.
"""

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from psycopg.types.json import Jsonb

from chemclaw.agent.audit import AuditEvent
from chemclaw.agent.leaver import erase_actor
from chemclaw.agent.session_fork import fork_session
from chemclaw.agent.session_store import (
    InMemoryHistoryProvider,
    PostgresHistoryProvider,
    stored_authorship,
)
from chemclaw.api.schemas import _transcript
from chemclaw.core import db
from chemclaw.core.authorship import UNNAMED_AGENT, Authorship
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.kg.note import Note, parse_note
from chemclaw.kg.record import record_note
from chemclaw.kg.render import render_note
from tests.conftest import FakeWriter
from tests.pg import create_checkpoint_tables, migrated_db_or_skip
from tests.test_session_fork import _seed

_MIGRATION = (
    Path(__file__).resolve().parents[1] / "infra" / "sql" / "109_session_message_authorship.sql"
)


# --- the model's encoding is the audit trail's -------------------------------------------------


def test_the_unnamed_agent_is_what_the_audit_trail_has_always_written() -> None:
    """The shared encoding was taken from `audit_events.agent`, so the trail needed no migration.

    If this ever disagrees, one of the three subsystems has started spelling "an agent, unnamed"
    differently from the others, and every row the trail already holds would read as a named agent
    called `""` — or as no agent at all — depending on which side moved.
    """
    event = AuditEvent(
        correlation_id="c", actor="oid-a", tool="t", arguments="", outcome="ok", latency_ms=0
    )
    assert event.agent == UNNAMED_AGENT == ""


# --- notes ------------------------------------------------------------------------------------


def _agent_note(actor: str | None = None) -> Note:
    """An agent-authored note, the kind `record_note` writes."""
    return Note(
        id="authorship-probe",
        type="observation",
        created_by="agent",
        body="A finding.",
        actor=actor,
    )


def test_a_note_written_before_actor_existed_reads_as_an_agent_with_nobody_recorded(
    tmp_path: Path,
) -> None:
    """Old frontmatter parses unchanged, and its authorship is the backfill rule, not a guess."""
    path = tmp_path / "old.md"
    path.write_text("---\nid: old-agent-note\ntype: observation\ncreated_by: agent\n---\nBody.\n")
    human = tmp_path / "curated.md"
    human.write_text("---\nid: curated-note\ntype: observation\ncreated_by: human\n---\nBody.\n")

    assert parse_note(path).authorship == Authorship(actor=None, agent=UNNAMED_AGENT)
    assert parse_note(human).authorship == Authorship(actor=None, agent=None)


def test_a_note_with_no_actor_renders_exactly_as_it_did() -> None:
    """Byte-stability is what the writer's "nothing staged" rule rests on (`kg/render.py`).

    A note written before the field existed must re-render without an `actor:` key, or every
    re-write of every existing note becomes a commit that changes nothing a person wrote.
    """
    assert "actor" not in render_note(_agent_note())
    assert "actor: oid-au-chemist" in render_note(_agent_note(actor="oid-au-chemist"))


async def test_record_note_stamps_the_person_whose_turn_wrote_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one write path names the turn's person on the file it commits — and nobody otherwise."""
    monkeypatch.setattr(settings, "note_repo_dir", str(tmp_path))
    writer = FakeWriter()

    tokens = set_current_identity("oid-au-chemist", frozenset())
    try:
        await record_note(_agent_note(), writer)
    finally:
        reset_current_identity(tokens)
    await record_note(_agent_note(), writer)

    stamped, unbound = (write.files[0].content for write in writer.writes)
    assert "actor: oid-au-chemist" in stamped
    assert "actor" not in unbound, "with no person bound the field must stay absent, not be guessed"


async def test_record_note_refuses_a_note_naming_somebody_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writing another person's id onto a note is forging half its provenance, like `created_by`."""
    monkeypatch.setattr(settings, "note_repo_dir", str(tmp_path))
    writer = FakeWriter()
    tokens = set_current_identity("oid-au-chemist", frozenset())
    try:
        with pytest.raises(ValueError, match="oid-au-other"):
            await record_note(_agent_note(actor="oid-au-other"), writer)
    finally:
        reset_current_identity(tokens)
    assert writer.writes == []


# --- session messages -------------------------------------------------------------------------


def _exchange() -> list[HumanMessage | AIMessage | ToolMessage]:
    """One turn's stored exchange: the chemist's words, a tool call, its result, the answer."""
    return [
        HumanMessage(content="what is the pKa?"),
        AIMessage(content="", tool_calls=[{"name": "predict_pka", "args": {}, "id": "call-1"}]),
        ToolMessage(content="4.2", tool_call_id="call-1"),
        AIMessage(content="About 4.2."),
    ]


_EXPECTED = [
    Authorship(actor="oid-au-chemist", agent=None),
    Authorship(actor="oid-au-chemist", agent=UNNAMED_AGENT),
    Authorship(actor="oid-au-chemist", agent=UNNAMED_AGENT),
    Authorship(actor="oid-au-chemist", agent=UNNAMED_AGENT),
]


async def test_a_stored_turn_names_the_chemist_for_every_message_and_the_agent_for_its_own() -> (
    None
):
    """Written by `save_messages`, read back by the transcript projection a surface renders."""
    await migrated_db_or_skip()
    provider = PostgresHistoryProvider()
    tokens = set_current_identity("oid-au-chemist", frozenset())
    try:
        await provider.save_messages("authorship-turn", _exchange())
    finally:
        reset_current_identity(tokens)

    stored = await provider.get_messages("authorship-turn")
    assert [stored_authorship(message) for message in stored] == _EXPECTED
    # The tool result is folded into its call, so the transcript shows three bubbles.
    assert [m.author for m in _transcript(stored)] == [_EXPECTED[0], _EXPECTED[1], _EXPECTED[3]]

    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT actor, agent FROM session_messages WHERE session_id = %s ORDER BY id",
            ("authorship-turn",),
        )
        assert await cur.fetchall() == [("oid-au-chemist", None), *[("oid-au-chemist", "")] * 3]


async def test_the_memory_store_answers_the_same_authorship() -> None:
    """A deployment on the in-memory store must render the same transcript as one on Postgres."""
    state: dict[str, object] = {}
    tokens = set_current_identity("oid-au-chemist", frozenset())
    try:
        await InMemoryHistoryProvider().save_messages("s", _exchange(), state=state)
    finally:
        reset_current_identity(tokens)
    stored = await InMemoryHistoryProvider().get_messages("s", state=state)
    assert [stored_authorship(message) for message in stored] == _EXPECTED


async def test_migration_109_backfills_the_owner_and_the_speaker_and_invents_nobody() -> None:
    """The backfill's rule, driven by the migration's own statements over pre-109 rows.

    Rows whose authorship is NULL/NULL are exactly what the table held before 109: the file is
    idempotent, so re-running it over them is the backfill. Both stored shapes, a session with no
    owner row and one with a NULL owner, so every branch of the rule has a row to decide.
    """
    await migrated_db_or_skip()
    rows = [
        ("bf-owned", {"type": "human", "data": {"content": "q"}}, "langchain"),
        ("bf-owned", {"type": "ai", "data": {"content": "a"}}, "langchain"),
        ("bf-owned", {"type": "tool", "data": {"content": "r", "tool_call_id": "x"}}, "langchain"),
        ("bf-owned", {"role": "user", "contents": []}, "maf"),
        ("bf-owned", {"role": "assistant", "contents": []}, "maf"),
        ("bf-orphan", {"type": "human", "data": {"content": "q"}}, "langchain"),
        ("bf-unowned", {"type": "ai", "data": {"content": "a"}}, "langchain"),
    ]
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO session_owners (session_id, owner) VALUES "
                "('bf-owned', 'oid-au-owner'), ('bf-unowned', NULL) ON CONFLICT DO NOTHING"
            )
            for session_id, message, shape in rows:
                await cur.execute(
                    "INSERT INTO session_messages (session_id, message, message_shape) "
                    "VALUES (%s, %s, %s)",
                    (session_id, Jsonb(message), shape),
                )
            await cur.execute(_MIGRATION.read_text())
            await cur.execute(
                "SELECT session_id, actor, agent FROM session_messages "
                "WHERE session_id LIKE 'bf-%%' ORDER BY id"
            )
            backfilled = await cur.fetchall()
        await conn.commit()

    assert backfilled == [
        ("bf-owned", "oid-au-owner", None),
        ("bf-owned", "oid-au-owner", ""),
        ("bf-owned", "oid-au-owner", ""),
        ("bf-owned", "oid-au-owner", None),
        ("bf-owned", "oid-au-owner", ""),
        ("bf-orphan", None, None),
        ("bf-unowned", None, ""),
    ]


async def test_a_fork_keeps_who_wrote_each_message_rather_than_naming_the_forker() -> None:
    """The copy is of what was said; attributing it to whoever forked would put words in a mouth."""
    await migrated_db_or_skip()
    await create_checkpoint_tables()
    await _seed("authorship-fork")
    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute(
            "UPDATE session_messages SET actor = 'oid-au-chemist', agent = NULL "
            "WHERE session_id = %s",
            ("authorship-fork",),
        )
        await conn.commit()

    child = await fork_session("authorship-fork", "oid-authorship-forker", None)

    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT actor, agent FROM session_messages WHERE session_id = %s", (child,)
        )
        assert await cur.fetchall() == [("oid-au-chemist", None)]


async def test_an_erasure_reaches_a_persons_words_in_a_session_somebody_else_owns() -> None:
    """The second arm of the erase statement: by author, not only by ownership.

    Unreachable today — every session has one person in it — and exactly the row a shared session
    will produce. The owner's own message beside it must survive, or the arm is a pattern match.
    """
    await migrated_db_or_skip()
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO session_owners (session_id, owner) VALUES ('shared-room', 'oid-host') "
                "ON CONFLICT (session_id) DO UPDATE SET owner = EXCLUDED.owner"
            )
            for actor, text in (("oid-guest", "the guest's words"), ("oid-host", "the host's")):
                await cur.execute(
                    "INSERT INTO session_messages (session_id, message, message_shape, actor) "
                    "VALUES ('shared-room', %s, 'langchain', %s)",
                    (Jsonb({"type": "human", "data": {"content": text}}), actor),
                )
        await conn.commit()

    report = await erase_actor("oid-guest", apply=True)

    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT actor FROM session_messages WHERE session_id = 'shared-room' ORDER BY id"
        )
        assert await cur.fetchall() == [("oid-host",)]
    assert report.erased["session_messages"] == 1
