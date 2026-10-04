"""Where a session's uploads live, what the store drops, and who can reach them.

Two defects, one file, because both are about what a chemist is told about a file they sent.

**Dropped silently.** The store evicts a session's oldest uploads past `attachment_max_per_session`
and past the byte budget, and for the whole life of that bound the eviction was *silent*: no log
record, no metric, no field on either model-facing tool. Measured at the shipped cap of 10 —
thirteen uploads, ten held, `plate-00/01/02.csv` gone, and `read_attachment("plate-00.csv")`
answering `no attachment named 'plate-00.csv' in this conversation` about a file the chemist had
just uploaded *to this conversation*. The model relays that as fact.

**Held by one replica.** The store was a dict in the memory of the pod that took the upload. With
two processes on one database, the second resolved the session (200) and answered
`read_attachment("runs.csv")` with the same false sentence — and the companion UI's BFF reaches the
front door through its Service, where the Route's affinity cookie that papered over this does not
exist (`D-2026-10-04-an-upload-is-session-state-not-pod-state`).

The behavioural tests run on both backends, because `InMemoryAttachmentStore` is a real backend —
what a deployment without durable sessions runs — and a failure names which one. The Postgres-only
tests are about what a database keeps: another process reading it, the session gate in front of
it, and the three disposals a conversation's rows follow.
"""

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from chemclaw.agent import attachments
from chemclaw.agent import session_members as members_module
from chemclaw.agent.attachments import (
    Attachment,
    AttachmentStore,
    InMemoryAttachmentStore,
    PostgresAttachmentStore,
    default_attachment_store,
    list_attachments,
    read_attachment,
)
from chemclaw.agent.leaver import erase_actor
from chemclaw.agent.session_store import SessionOwnerStore
from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.session_context import set_current_session_id
from chemclaw.durable.retention import prune_expired_rows
from tests.pg import migrated_db_or_skip
from tests.test_service import _no_connectors

_BACKENDS = ("memory", "postgres")

_ANA = Principal(oid="ana-uploads", upn="ana@corp", roles=frozenset({"process-chemist"}))
_BEN = Principal(oid="ben-uploads", upn="ben@corp", roles=frozenset({"analyst"}))
_CAT = Principal(oid="cat-uploads", upn="cat@corp", roles=frozenset({"analyst"}))


async def _backend(name: str) -> AttachmentStore:
    """A store of the named kind, skipping when no database is reachable."""
    if name == "postgres":
        await migrated_db_or_skip()
        return PostgresAttachmentStore()
    return InMemoryAttachmentStore()


def _session() -> str:
    """A session id nothing else in the suite writes under."""
    return uuid4().hex


def _file(name: str, text: str = "a,b\n1,2\n") -> Attachment:
    """One parsed upload, small enough that only the count bound can bite."""
    return Attachment(name=name, content_type="text/csv", text=text, rows=1)


@pytest.fixture
def bound_to(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Point the two tools at `store` and bind `session` as the turn's, for one test."""

    def bind(store: AttachmentStore, session: str) -> None:
        monkeypatch.setattr(attachments, "default_attachment_store", lambda: store)
        set_current_session_id(session)

    yield bind
    # Cleared rather than reset by token: an async test binds inside its own task's context, which
    # a token from there cannot be reset outside of, and a sync test binds in this one.
    set_current_session_id(None)


# --- what the store drops, and that it says so -------------------------------------------------


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_a_session_remembers_which_uploads_it_dropped(backend: str) -> None:
    """The count bound evicts, and the store used to keep no record that it had.

    Thirteen uploads at the shipped cap of ten: the three oldest are gone from `items`, and the
    only honest thing left to say about them is that they *were* here.
    """
    store, session = await _backend(backend), _session()
    for index in range(settings.attachment_max_per_session + 3):
        await store.add(session, _file(f"plate-{index:02d}.csv"), uploaded_by=_ANA.oid)

    held = await store.snapshot(session)
    assert len(held.items) == settings.attachment_max_per_session
    assert held.items[-1].name == f"plate-{settings.attachment_max_per_session + 2:02d}.csv"
    assert held.evicted == ["plate-00.csv", "plate-01.csv", "plate-02.csv"]
    assert held.evicted_total == 3


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_the_byte_bound_records_its_evictions_too(backend: str) -> None:
    """Both per-session bounds drop the same way, so both must leave the same record.

    The byte half is the one that bites on real spreadsheets: `attachment_max_bytes` bounds the
    *upload* and the parsed expansion is bounded by `document_max_expanded_bytes`, which is larger
    than the per-session budget. ASCII text, so resident bytes (memory) and stored UTF-8 bytes
    (Postgres) differ only by an object header, and the files are one byte over a third each so
    the budget is breached on both counts rather than on the header alone.
    """
    store, session = await _backend(backend), _session()
    per_file = settings.attachment_store_max_bytes // 3 + 1
    for index in range(3):
        await store.add(session, _file(f"big{index}.csv", "x" * per_file), uploaded_by=_ANA.oid)

    held = await store.snapshot(session, excerpt_chars=0)
    assert [item.name for item in held.items] == ["big1.csv", "big2.csv"]
    assert held.evicted == ["big0.csv"]


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_only_the_newest_dropped_names_are_remembered_and_the_count_is_whole(
    backend: str,
) -> None:
    """Names are bounded for the model's context; the total is not, so a long list still says so."""
    store, session = await _backend(backend), _session()
    remembered = attachments._EVICTED_NAMES_REMEMBERED
    uploads = settings.attachment_max_per_session + remembered + 5
    for index in range(uploads):
        await store.add(session, _file(f"f{index:03d}.csv"), uploaded_by=_ANA.oid)

    held = await store.snapshot(session, excerpt_chars=0)
    assert held.evicted_total == remembered + 5
    assert held.evicted == [f"f{index:03d}.csv" for index in range(5, remembered + 5)]


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_a_listing_reads_excerpts_and_a_read_reads_the_whole_file(backend: str) -> None:
    """The listing must not cost a session's whole working set; the read must not be cut."""
    store, session = await _backend(backend), _session()
    await store.add(session, _file("long.csv", "y" * 5000), uploaded_by=_ANA.oid)

    listed = await store.snapshot(session, excerpt_chars=10)
    assert [item.text for item in listed.items] == ["y" * 10]
    found = await store.find(session, "long.csv")
    assert found is not None and found.text == "y" * 5000
    assert await store.find(session, "absent.csv") is None
    assert await store.find(_session(), "long.csv") is None, "another session's file was found"


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_the_listing_says_what_it_is_not_showing(backend: str, bound_to: Any) -> None:
    """`list_attachments` claimed "everything attached to a session" over a truncated list.

    The verdict is a `computed_field` rather than a property for the reason
    `FingerprintSearch.verdict` is: a bare property is not serialized, so the one sentence that
    tells the model its list is short would never leave this process.
    """
    store, session = await _backend(backend), _session()
    bound_to(store, session)
    for index in range(settings.attachment_max_per_session + 3):
        await store.add(session, _file(f"plate-{index:02d}.csv"), uploaded_by=_ANA.oid)
    listing = await list_attachments()

    assert len(listing.attachments) == settings.attachment_max_per_session
    assert listing.evicted == ["plate-00.csv", "plate-01.csv", "plate-02.csv"]
    assert listing.evicted_total == 3
    payload = listing.model_dump()
    assert "verdict" in payload  # serialized, not a bare property
    assert "3" in payload["verdict"]
    assert "plate-00.csv" in payload["verdict"]


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_the_listing_verdict_says_nothing_was_dropped_when_nothing_was(
    backend: str, bound_to: Any
) -> None:
    """The ordinary case must read as complete, or the marker means nothing."""
    store, session = await _backend(backend), _session()
    bound_to(store, session)
    await store.add(session, _file("only.csv"), uploaded_by=_ANA.oid)
    listing = await list_attachments()

    assert [a.name for a in listing.attachments] == ["only.csv"]
    assert listing.evicted == []
    assert listing.evicted_total == 0
    assert "COMPLETE" in listing.model_dump()["verdict"]


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_reading_a_dropped_upload_says_it_was_dropped_not_that_it_never_arrived(
    backend: str, bound_to: Any
) -> None:
    """The worst half: silence became a false statement about the chemist.

    `no attachment named 'plate-00.csv' in this conversation` is a claim, and it is wrong — the
    file was uploaded to this conversation and this store dropped it. A model told that tells the
    chemist they never sent it.
    """
    store, session = await _backend(backend), _session()
    bound_to(store, session)
    for index in range(settings.attachment_max_per_session + 1):
        await store.add(session, _file(f"plate-{index:02d}.csv"), uploaded_by=_ANA.oid)
    with pytest.raises(ValueError) as dropped:
        await read_attachment("plate-00.csv")
    with pytest.raises(ValueError) as absent:
        await read_attachment("never-sent.csv")

    assert "dropped" in str(dropped.value)
    assert "was uploaded" in str(dropped.value)
    # ...and the two cases must not render alike, which is the whole point.
    assert "was uploaded" not in str(absent.value)
    assert str(dropped.value) != str(absent.value)


def test_the_store_follows_the_session_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Durable sessions get the durable store; a memory deployment keeps one in-process store."""
    monkeypatch.setattr(settings, "session_store", "postgres")
    assert isinstance(default_attachment_store(), PostgresAttachmentStore)
    monkeypatch.setattr(settings, "session_store", "memory")
    assert default_attachment_store() is default_attachment_store()
    assert isinstance(default_attachment_store(), InMemoryAttachmentStore)
    assert isinstance(default_attachment_store(), AttachmentStore)


async def test_concurrent_uploads_to_one_session_do_not_overrun_its_bound() -> None:
    """The eviction pass reads the session's rows and decides from them, so it is serialized.

    Without the per-session lock, uploads racing on one session each count the others' rows as
    absent and all keep them: the session ends over its cap with nothing recorded as dropped.
    """
    await migrated_db_or_skip()
    store, session = PostgresAttachmentStore(), _session()
    total = settings.attachment_max_per_session * 3
    await asyncio.gather(
        *(
            store.add(session, _file(f"c{index}.csv"), uploaded_by=_ANA.oid)
            for index in range(total)
        )
    )
    held = await store.snapshot(session, excerpt_chars=0)
    assert len(held.items) == settings.attachment_max_per_session
    assert held.evicted_total == total - settings.attachment_max_per_session


# --- every replica reads the same uploads, and only the session's participants -----------------


def _as(app: Any, principal: Principal) -> TestClient:
    """A client whose requests arrive as `principal` — authentication is not under test."""
    app.dependency_overrides[require_principal] = lambda: principal
    return TestClient(app)


@pytest.fixture
def durable_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The production front door on durable sessions, with its real ownership and member stores."""
    asyncio.run(migrated_db_or_skip())
    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "entra_required", True)
    members_module.session_member_store.cache_clear()
    yield create_app(connector_factory=_no_connectors)
    members_module.session_member_store.cache_clear()


def _upload(app: Any, principal: Principal, session: str, name: str, body: bytes) -> Any:
    """POST one CSV to a session's attachment route, as `principal`."""
    return _as(app, principal).post(
        f"/sessions/{session}/attachments", files={"file": (name, body, "text/csv")}
    )


# What a second replica runs: a fresh interpreter on the same database, with nothing of the first
# process's memory, asking the two tools about the session exactly as a turn there would.
_REPLICA = """
import asyncio, json, sys
from chemclaw.agent.attachments import list_attachments, read_attachment
from chemclaw.core.session_context import set_current_session_id

set_current_session_id(sys.argv[1])

async def main() -> None:
    listing = await list_attachments()
    print(json.dumps({
        "listed": [a.name for a in listing.attachments],
        "read": await read_attachment(sys.argv[2]),
    }))

asyncio.run(main())
"""


def _on_another_replica(session: str, name: str) -> dict[str, Any]:
    """Run `_REPLICA` in a separate process pointed at this test's database and schema."""
    env = {
        **os.environ,
        "CHEMCLAW_SESSION_STORE": "postgres",
        "CHEMCLAW_POSTGRES_DSN": settings.postgres_dsn,
        "CHEMCLAW_SESSION_STORE_DSN": settings.session_store_dsn,
    }
    done = subprocess.run(
        [sys.executable, "-c", _REPLICA, session, name],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    answer: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
    return answer


def test_an_upload_taken_by_one_replica_is_read_by_another(durable_app: Any) -> None:
    """The defect, end to end: upload through one process, read it from a different one.

    Before `session_attachments` the second process answered "no attachment named 'runs.csv' in
    this conversation". Nothing of the upload may be left in this process's memory either, or the
    first half of the proof would be reading a copy only this replica has.
    """
    session = str(_as(durable_app, _ANA).post("/sessions").json()["session_id"])
    uploaded = _upload(durable_app, _ANA, session, "runs.csv", b"temp,yield\n80,0.91\n")
    assert uploaded.status_code == 200, uploaded.text
    assert asyncio.run(attachments.STORE.snapshot(session)).items == [], (
        "the upload went to this process's memory, which no other replica can read"
    )

    answer = _on_another_replica(session, "runs.csv")
    assert answer["listed"] == ["runs.csv"]
    assert "0.91" in answer["read"]


def test_a_stranger_cannot_upload_into_or_read_from_somebody_elses_session(
    durable_app: Any, bound_to: Any
) -> None:
    """The session id in the path is a client's claim; the session gate is what answers it.

    A stranger's upload into Ana's session is a 404 indistinguishable from an unknown session, and
    nothing is stored. A stranger's own conversation reads its own uploads and never Ana's, because
    the tools read only the turn's bound session — there is no id a model or a client can pass
    that names another one.
    """
    session = str(_as(durable_app, _ANA).post("/sessions").json()["session_id"])
    assert _upload(durable_app, _ANA, session, "ana.csv", b"secret,1\n").status_code == 200
    refused = _upload(durable_app, _CAT, session, "cat.csv", b"x,1\n")
    assert refused.status_code == 404, refused.text
    assert refused.json()["detail"] == "unknown session"

    own = str(_as(durable_app, _CAT).post("/sessions").json()["session_id"])
    assert _upload(durable_app, _CAT, own, "cat.csv", b"x,1\n").status_code == 200
    bound_to(PostgresAttachmentStore(), own)
    listing = asyncio.run(list_attachments())
    assert [a.name for a in listing.attachments] == ["cat.csv"]
    with pytest.raises(ValueError, match="no attachment named 'ana.csv'"):
        asyncio.run(read_attachment("ana.csv"))
    held = asyncio.run(PostgresAttachmentStore().snapshot(session, excerpt_chars=0))
    assert [a.name for a in held.items] == ["ana.csv"], "the refused upload was stored"


def test_a_member_uploads_into_a_shared_session_under_their_own_name(durable_app: Any) -> None:
    """Membership is what admits an upload, and the row names who made it — for the erasure."""
    session = str(_as(durable_app, _ANA).post("/sessions").json()["session_id"])
    assert _as(durable_app, _ANA).put(f"/sessions/{session}/members/{_BEN.oid}").status_code == 204
    assert _upload(durable_app, _BEN, session, "ben.csv", b"b,1\n").status_code == 200
    assert asyncio.run(_uploaders(session)) == [("ben.csv", _BEN.oid)]


# --- the three disposals a conversation's rows follow ------------------------------------------


async def _uploaders(session: str) -> list[tuple[str, str]]:
    """Every row a session has, held or dropped, as `(name, uploaded_by)` in upload order."""
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT name, uploaded_by FROM session_attachments WHERE session_id = %s "
                "ORDER BY attachment_id",
                (session,),
            )
            return [(str(row[0]), str(row[1])) for row in await cur.fetchall()]


async def _owned(owner: str) -> str:
    """A session with an ownership row, as a real one has."""
    session = _session()
    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute(
            "INSERT INTO session_owners (session_id, owner) VALUES (%s, %s)", (session, owner)
        )
        await conn.commit()
    return session


async def test_deleting_a_session_deletes_its_uploads_and_no_others() -> None:
    """`delete_session` takes every upload of the conversation, held or dropped, whoever sent it."""
    await migrated_db_or_skip()
    store = PostgresAttachmentStore()
    doomed, kept = await _owned("del-uploader"), await _owned("del-uploader")
    for index in range(settings.attachment_max_per_session + 1):
        await store.add(doomed, _file(f"d{index}.csv"), uploaded_by="del-uploader")
    await store.add(doomed, _file("guest.csv"), uploaded_by="a-member")
    await store.add(kept, _file("k.csv"), uploaded_by="del-uploader")

    removed = await SessionOwnerStore().delete_session(doomed)
    assert removed["session_attachments"] == settings.attachment_max_per_session + 2
    assert await _uploaders(doomed) == []
    assert await _uploaders(kept) == [("k.csv", "del-uploader")]


async def test_an_erasure_takes_the_leavers_uploads_in_their_sessions_and_in_others() -> None:
    """By session for the leaver's own conversations, and by uploader in anybody else's."""
    await migrated_db_or_skip()
    store = PostgresAttachmentStore()
    leaver, other = f"oid-x-{uuid4().hex[:6]}", f"oid-y-{uuid4().hex[:6]}"
    theirs, others = await _owned(leaver), await _owned(other)
    await store.add(theirs, _file("mine.csv"), uploaded_by=leaver)
    await store.add(theirs, _file("visitor.csv"), uploaded_by=other)
    await store.add(others, _file("handed-over.csv"), uploaded_by=leaver)
    await store.add(others, _file("owners.csv"), uploaded_by=other)

    report = await erase_actor(leaver, apply=True)
    assert report.erased["session_attachments"] == 3
    assert await _uploaders(theirs) == []
    assert await _uploaders(others) == [("owners.csv", other)]


async def test_the_retention_sweep_ages_out_uploads_on_the_conversations_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An upload is a turn's input, so it is kept exactly as long as the conversation's messages."""
    await migrated_db_or_skip()
    for name in (
        "retention_session_events_days",
        "retention_tool_results_days",
        "retention_result_publications_days",
        "retention_checkpoints_days",
        "retention_session_exhibits_days",
    ):
        monkeypatch.setattr(settings, name, 0)
    monkeypatch.setattr(settings, "retention_session_messages_days", 30)
    store = PostgresAttachmentStore()
    session = await _owned("ret-uploader")
    await store.add(session, _file("old.csv"), uploaded_by="ret-uploader")
    await store.add(session, _file("fresh.csv"), uploaded_by="ret-uploader")
    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute(
            "UPDATE session_attachments SET created_at = now() - interval '40 days' "
            "WHERE session_id = %s AND name = 'old.csv'",
            (session,),
        )
        await conn.commit()

    outcome = await prune_expired_rows()
    assert outcome.deleted.get("session_attachments", 0) >= 1
    assert await _uploaders(session) == [("fresh.csv", "ret-uploader")]
