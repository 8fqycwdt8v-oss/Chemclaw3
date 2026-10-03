"""The artefact store on both backends, and the lifecycle a conversation's artefacts follow.

`InMemoryExhibitStore` is a real backend — what a deployment without Postgres runs on — so every
behavioural test is parametrized over both, and a failure names which one. The Postgres-only tests
are the ones about rows a database keeps: the cascade from header to revisions on a session delete,
an erasure, and a retention sweep.
"""

from typing import Any
from uuid import uuid4

import pytest

from chemclaw.agent.leaver import erase_actor
from chemclaw.agent.session_store import SessionOwnerStore
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.durable.retention import prune_expired_rows
from chemclaw.exhibits.models import (
    ExhibitLimit,
    InvalidExhibit,
    Spec,
    StaleRevision,
    UnknownExhibit,
    parse_spec,
)
from chemclaw.exhibits.store import (
    ExhibitStore,
    InMemoryExhibitStore,
    PostgresExhibitStore,
    default_exhibit_store,
)
from tests.pg import migrated_db_or_skip

_BACKENDS = ("memory", "postgres")


async def _backend(name: str) -> ExhibitStore:
    """A store of the named kind, skipping when no database is reachable."""
    if name == "postgres":
        await migrated_db_or_skip()
        return PostgresExhibitStore()
    return InMemoryExhibitStore()


def _session() -> str:
    """A session id nothing else in the suite writes under."""
    return uuid4().hex


def _doc(markdown: str) -> Spec:
    return parse_spec({"kind": "document", "markdown": markdown})


def _table(value: float) -> Spec:
    return parse_spec(
        {"kind": "table", "columns": [{"key": "y", "label": "Yield"}], "rows": [{"y": value}]}
    )


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_a_revision_is_appended_and_both_revisions_stay_readable(backend: str) -> None:
    """Revision 1 by the agent, revision 2 by a person; the header tracks the head."""
    store = await _backend(backend)
    session = _session()
    created = await store.create(
        session, title="Plan", spec=_doc("one"), author_kind="agent", author="ana"
    )
    assert (created.revision, created.parent_revision, created.head_revision) == (1, 0, 1)
    revised = await store.append(
        session,
        created.exhibit_id,
        spec=_doc("two"),
        parent_revision=1,
        author_kind="human",
        author="ben",
        change_note="tightened",
        title="Plan v2",
    )
    assert (revised.revision, revised.parent_revision) == (2, 1)
    assert (revised.head_author_kind, revised.head_author, revised.title) == (
        "human",
        "ben",
        "Plan v2",
    )

    first = await store.view(session, created.exhibit_id, 1)
    assert first is not None and first.spec == _doc("one")
    # A past revision is served with the *current* header beside its own record.
    assert first.head_revision == 2 and first.author == "ana"
    head = await store.view(session, created.exhibit_id)
    assert head is not None and head.spec == _doc("two")
    history = await store.revisions(session, created.exhibit_id)
    assert history is not None
    assert [(r.revision, r.author_kind, r.change_note) for r in history] == [
        (1, "agent", ""),
        (2, "human", "tightened"),
    ]
    assert all(r.byte_size > 0 for r in history)


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_a_write_on_a_stale_base_is_refused_naming_the_head(backend: str) -> None:
    """The chemist's edit landed first; the agent's write on revision 1 must not discard it."""
    store = await _backend(backend)
    session = _session()
    made = await store.create(session, title="T", spec=_table(1), author_kind="agent", author="a")
    await store.append(
        session, made.exhibit_id, spec=_table(2), parent_revision=1, author_kind="human", author="b"
    )
    with pytest.raises(StaleRevision) as refused:
        await store.append(
            session,
            made.exhibit_id,
            spec=_table(3),
            parent_revision=1,
            author_kind="agent",
            author="a",
        )
    assert refused.value.head == 2
    head = await store.view(session, made.exhibit_id)
    assert head is not None and head.spec == _table(2)


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_another_sessions_artefact_answers_as_an_unknown_one(backend: str) -> None:
    """Every read and write is session-scoped; nothing distinguishes "not yours" from "none"."""
    store = await _backend(backend)
    mine, theirs = _session(), _session()
    made = await store.create(mine, title="T", spec=_doc("x"), author_kind="agent", author="a")
    assert await store.view(theirs, made.exhibit_id) is None
    assert await store.revisions(theirs, made.exhibit_id) is None
    assert await store.headers(theirs) == []
    with pytest.raises(UnknownExhibit):
        await store.append(
            theirs,
            made.exhibit_id,
            spec=_doc("y"),
            parent_revision=1,
            author_kind="human",
            author="b",
        )
    assert await store.view(mine, made.exhibit_id, 7) is None


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_a_revision_may_not_change_what_the_artefact_is(backend: str) -> None:
    """A table stays a table; a different kind is a new artefact."""
    store = await _backend(backend)
    session = _session()
    made = await store.create(session, title="T", spec=_table(1), author_kind="agent", author="a")
    with pytest.raises(InvalidExhibit, match="table artefact"):
        await store.append(
            session,
            made.exhibit_id,
            spec=_doc("x"),
            parent_revision=1,
            author_kind="agent",
            author="a",
        )


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_a_session_at_its_cap_is_refused_another(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap refuses rather than evicting an artefact somebody may still be reading."""
    store = await _backend(backend)
    monkeypatch.setattr(settings, "exhibit_max_per_session", 2)
    session = _session()
    for title in ("a", "b"):
        await store.create(session, title=title, spec=_doc(title), author_kind="agent", author="x")
    with pytest.raises(ExhibitLimit, match="revise an existing artefact"):
        await store.create(session, title="c", spec=_doc("c"), author_kind="agent", author="x")
    # The cap is per session.
    await store.create(_session(), title="c", spec=_doc("c"), author_kind="agent", author="x")


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_a_chosen_id_makes_create_a_create_or_return(backend: str) -> None:
    """The same id twice is one artefact; another session's id and a malformed one are refused.

    What a durable writer's retry relies on (`durable/report_workflow.record_report_exhibit`):
    the second call returns revision 1 and writes nothing, even with a different spec.
    """
    store = await _backend(backend)
    session = _session()
    chosen = f"xb-{uuid4().hex[:16]}"
    first = await store.create(
        session, title="r", spec=_doc("one"), author_kind="agent", author="x", exhibit_id=chosen
    )
    again = await store.create(
        session, title="r2", spec=_doc("two"), author_kind="agent", author="x", exhibit_id=chosen
    )
    assert first.exhibit_id == again.exhibit_id == chosen
    assert (again.revision, again.title, again.spec) == (1, "r", _doc("one"))
    assert [header.exhibit_id for header in await store.headers(session)] == [chosen]
    with pytest.raises(InvalidExhibit, match="another conversation"):
        await store.create(
            _session(),
            title="r",
            spec=_doc("x"),
            author_kind="agent",
            author="x",
            exhibit_id=chosen,
        )
    with pytest.raises(InvalidExhibit, match="not an artefact id"):
        await store.create(
            session, title="r", spec=_doc("x"), author_kind="agent", author="x", exhibit_id="xb-1"
        )


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_the_agents_read_mark_follows_its_writes_and_never_moves_back(backend: str) -> None:
    """What the turn note reads: an agent write moves the mark, a person's does not."""
    store = await _backend(backend)
    session = _session()
    made = await store.create(session, title="T", spec=_doc("1"), author_kind="agent", author="a")
    await store.append(
        session, made.exhibit_id, spec=_doc("2"), parent_revision=1, author_kind="human", author="b"
    )
    (state,) = await store.states(session)
    assert (state.agent_seen_revision, state.last_agent_revision) == (1, 1)
    assert state.header.head_revision == 2
    await store.mark_seen(session, made.exhibit_id, 2)
    await store.mark_seen(session, made.exhibit_id, 1)
    (state,) = await store.states(session)
    assert state.agent_seen_revision == 2
    await store.append(
        session, made.exhibit_id, spec=_doc("3"), parent_revision=2, author_kind="agent", author="a"
    )
    (state,) = await store.states(session)
    assert (state.agent_seen_revision, state.last_agent_revision) == (3, 3)
    pinned = await store.create(session, title="P", spec=_doc("p"), author_kind="human", author="b")
    states = {s.header.exhibit_id: s for s in await store.states(session)}
    assert states[pinned.exhibit_id].agent_seen_revision == 0
    # Newest update first.
    assert [s.header.exhibit_id for s in await store.states(session)][0] == pinned.exhibit_id


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_an_artefact_at_its_revision_cap_is_refused_another(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every revision is a whole spec kept for the history, so the history has a ceiling."""
    store = await _backend(backend)
    monkeypatch.setattr(settings, "exhibit_max_revisions", 2)
    session = _session()
    made = await store.create(session, title="T", spec=_table(1), author_kind="agent", author="a")
    await store.append(
        session, made.exhibit_id, spec=_table(2), parent_revision=1, author_kind="human", author="b"
    )
    with pytest.raises(ExhibitLimit, match="create a new artefact"):
        await store.append(
            session,
            made.exhibit_id,
            spec=_table(3),
            parent_revision=2,
            author_kind="agent",
            author="a",
        )
    assert (await store.view(session, made.exhibit_id)).head_revision == 2  # type: ignore[union-attr]


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_the_chemists_figures_are_read_as_recorded_each_once(backend: str) -> None:
    """What the grounding check reads: each figure a person's revision recorded, once, in order.

    Recorded at write time, so the read parses no spec — the check that runs on every agent write
    used to parse every person's revision and its parent. An agent revision's figures and an
    unrecorded (pre-119) person's revision count for nothing; another session's are unknown.
    """
    store = await _backend(backend)
    session = _session()
    made = await store.create(
        session,
        title="T",
        spec=_table(1),
        author_kind="human",
        author="b",
        chemist_figures=["4.76", "9.95"],
    )
    writes: list[tuple[str, list[str] | None]] = [
        ("agent", ["1.5"]),
        ("human", ["9.95", "16"]),
        ("human", None),
    ]
    for revision, (kind, figures) in enumerate(writes, start=1):
        await store.append(
            session,
            made.exhibit_id,
            spec=_table(revision + 1),
            parent_revision=revision,
            author_kind=kind,  # type: ignore[arg-type]
            author=kind,
            chemist_figures=figures,
        )
    assert await store.chemist_figures(session, made.exhibit_id) == ["4.76", "9.95", "16"]
    assert await store.chemist_figures(_session(), made.exhibit_id) == [], (
        "another session's is unknown"
    )


def test_the_default_store_follows_the_session_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Postgres where sessions are durable, the process-lifetime memory store otherwise."""
    monkeypatch.setattr(settings, "session_store", "postgres")
    assert isinstance(default_exhibit_store(), PostgresExhibitStore)
    monkeypatch.setattr(settings, "session_store", "memory")
    assert default_exhibit_store() is default_exhibit_store()
    assert isinstance(default_exhibit_store(), ExhibitStore)


# --- the conversation's lifecycle, against a real database ------------------------------------


async def _owned(owner: str) -> str:
    """A session with an ownership row and a message, as a real one has."""
    session = _session()
    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute(
            "INSERT INTO session_owners (session_id, owner) VALUES (%s, %s)", (session, owner)
        )
        await conn.commit()
    return session


async def _rows(exhibit_id: str) -> tuple[int, int]:
    """How many header and revision rows an artefact still has."""
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM session_exhibits WHERE exhibit_id = %s", (exhibit_id,)
            )
            headers = await cur.fetchone()
            await cur.execute(
                "SELECT count(*) FROM session_exhibit_revisions WHERE exhibit_id = %s",
                (exhibit_id,),
            )
            revisions = await cur.fetchone()
    return int(headers[0] if headers else 0), int(revisions[0] if revisions else 0)


async def _two_revisions(session: str) -> str:
    """An artefact with an agent revision and a person's on top, so the cascade has rows to take."""
    store = PostgresExhibitStore()
    made = await store.create(session, title="T", spec=_doc("1"), author_kind="agent", author="o")
    await store.append(
        session, made.exhibit_id, spec=_doc("2"), parent_revision=1, author_kind="human", author="o"
    )
    return made.exhibit_id


async def test_deleting_a_session_deletes_its_artefacts_and_their_revisions() -> None:
    """`delete_session` takes the header and the cascade takes every revision; others stay."""
    await migrated_db_or_skip()
    doomed, kept = await _owned("del-owner"), await _owned("del-owner")
    gone, stays = await _two_revisions(doomed), await _two_revisions(kept)
    removed = await SessionOwnerStore().delete_session(doomed)
    assert removed["session_exhibits"] == 1
    assert await _rows(gone) == (0, 0)
    assert await _rows(stays) == (1, 2)


async def test_an_erasure_takes_the_leavers_artefacts_and_reports_them() -> None:
    """Artefacts follow `session_messages`: erased with the conversation, counted in the report."""
    await migrated_db_or_skip()
    leaver, other = f"oid-x-{uuid4().hex[:6]}", f"oid-y-{uuid4().hex[:6]}"
    theirs, others = await _owned(leaver), await _owned(other)
    gone, stays = await _two_revisions(theirs), await _two_revisions(others)
    report = await erase_actor(leaver, apply=True)
    assert report.erased["session_exhibits"] == 1
    assert await _rows(gone) == (0, 0)
    assert await _rows(stays) == (1, 2)


async def test_the_retention_sweep_ages_out_an_artefact_by_its_last_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dated by `updated_at`, so an artefact still being edited is never taken from under anyone."""
    await migrated_db_or_skip()
    for name in (
        "retention_session_messages_days",
        "retention_session_events_days",
        "retention_tool_results_days",
        "retention_result_publications_days",
        "retention_checkpoints_days",
    ):
        monkeypatch.setattr(settings, name, 0)
    monkeypatch.setattr(settings, "retention_session_exhibits_days", 30)
    session = await _owned("ret-owner")
    old, fresh = await _two_revisions(session), await _two_revisions(session)
    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute(
            "UPDATE session_exhibits SET updated_at = now() - interval '40 days' "
            "WHERE exhibit_id = %s",
            (old,),
        )
        await conn.commit()
    outcome = await prune_expired_rows()
    assert outcome.deleted.get("session_exhibits", 0) >= 1
    assert await _rows(old) == (0, 0)
    assert await _rows(fresh) == (1, 2)


async def test_the_listing_across_sessions_covers_owned_and_joined_and_nothing_else() -> None:
    """`GET /exhibits`' query: the caller's own sessions and the ones they were let into."""
    await migrated_db_or_skip()
    ana, ben = f"oid-a-{uuid4().hex[:6]}", f"oid-b-{uuid4().hex[:6]}"
    own, joined, foreign = await _owned(ana), await _owned(ben), await _owned(ben)
    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute(
            "INSERT INTO session_members (session_id, actor) VALUES (%s, %s)", (joined, ana)
        )
        await conn.commit()
    ids: dict[str, Any] = {
        name: await _two_revisions(session)
        for name, session in (("own", own), ("joined", joined), ("foreign", foreign))
    }
    listed = await PostgresExhibitStore().listing_for(ana, 50)
    assert {header.exhibit_id for header in listed} == {ids["own"], ids["joined"]}
    assert len(await PostgresExhibitStore().listing_for(ana, 1)) == 1
