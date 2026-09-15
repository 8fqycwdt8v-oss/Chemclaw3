"""An answer days later is answered against a corpus that moved.

ADR: `D-2026-09-15-an-answer-days-later-is-answered-against-a-corpus-that-moved`.

A durable wait holds a question open for up to 90 days and the BO case deliberately waits a week for
plates. Nothing asked, before releasing the workflow, whether the knowledge the question rested on
was still standing. These tests are the two ends of the control that closes it: the ask refuses a
premise that is already broken, and the answer refuses one that broke while it waited.

**The two halves are a pair, and testing either alone proves nothing.** Without the ask-time
refusal, an answer-time break is indistinguishable from a note that was already retired when the
question was written — this tree has no arrival signal for a note, so "since" is established by the
ask being refused, not by a timestamp. `test_a_question_on_retired_knowledge_is_refused_at_the_ask`
is therefore load-bearing for the *other* test's claim, not a separate nicety.

The corpus is built on disk the way `tests/test_digest.py` builds one —
`note_repo_dir/knowledge_dir`, the layout a pod actually has — because `build_graph` reads
files, and a fake would prove only that the fake was consulted.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.errors import ChemclawError
from chemclaw.durable import pending_store
from chemclaw.kg.graph import invalidate_cache
from chemclaw.kg.note import Note
from chemclaw.kg.premise import premise_breaks
from chemclaw.kg.render import render_note
from tests.pg import migrated_db_or_skip

_ALICE = Principal(oid="u-alice", upn="alice@example.com", roles=frozenset())


def _no_connectors(profile: str | None = None) -> list[object]:
    """No connector session: nothing here reaches a capability server."""
    return []


def _no_graph(*args: object, **kwargs: object) -> None:
    """No agent graph: these tests drive the pending routes, never a turn."""
    return None


def _corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *notes: Note) -> None:
    """Lay these notes out as a pod's knowledge checkout and point the settings at it."""
    repo = tmp_path / "note-repo"
    for note in notes:
        path = repo / "knowledge" / note.type / f"{note.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_note(note), encoding="utf-8")
    monkeypatch.setattr(settings, "note_repo_dir", str(repo))
    monkeypatch.setattr(settings, "knowledge_dir", "knowledge")
    invalidate_cache()


def _standing() -> Note:
    """A note that is current: no `valid_to`, so `is_current` holds on any date."""
    return Note(
        id="playbook-degassing",
        type="playbook",
        body="Degas by three freeze-pump-thaw cycles before adding the catalyst.",
    )


def _retired() -> Note:
    """The same note after a synthesis superseded it — `valid_to` in the past."""
    return Note(
        id="playbook-degassing",
        type="playbook",
        body="Degas by three freeze-pump-thaw cycles before adding the catalyst.",
        valid_from=date(2026, 1, 1),
        valid_to=date(2026, 6, 1),
    )


def test_a_standing_note_is_not_a_break(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The negative case first: a whole premise must not refuse anything."""
    _corpus(tmp_path, monkeypatch, _standing())
    assert asyncio.run(premise_breaks(["playbook-degassing"])) == []


def test_a_retired_note_breaks_the_premise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `valid_to` in the past is what `retire_note` and `close_refuted_note` both write."""
    _corpus(tmp_path, monkeypatch, _retired())
    (broken,) = asyncio.run(premise_breaks(["playbook-degassing"], as_of=date(2026, 9, 15)))
    assert broken.reason == "retired"
    assert "superseded or refuted" in broken.describe()


def test_an_id_that_resolves_to_nothing_breaks_the_premise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`note_in`, not `in graph`, and this is the test that tells them apart.

    `_assemble_graph` mints a bare node for every cited-but-undefined id, so `note_id in graph` is
    `True` for exactly the ids that resolve to nothing. A premise check written with the membership
    test would report a whole premise for a note that is not there — the failure mode inverted.
    The standing note's body cites the missing id, which is what puts that bare node in the graph.
    """
    citing = Note(
        id="playbook-degassing",
        type="playbook",
        body="Superseded in part by [[playbook-sparging]], which is not in this corpus.",
    )
    _corpus(tmp_path, monkeypatch, citing)
    (broken,) = asyncio.run(premise_breaks(["playbook-sparging"]))
    assert broken.reason == "absent"


def test_an_empty_premise_never_reads_the_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most questions cite nothing; a graph build for each of them would be a tax on all of them.

    Asserted by breaking the scan rather than by timing it — a fast path that still called through
    would pass a timing test on a small corpus and cost 1.2 s on a real one.
    """
    from chemclaw.kg import premise

    def _never(note_ids: list[str], as_of: date) -> list[object]:
        raise AssertionError("an empty premise must not reach the corpus")

    monkeypatch.setattr(premise, "_breaks", _never)
    assert asyncio.run(premise_breaks([])) == []


def test_a_question_on_retired_knowledge_is_refused_at_the_ask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ask-time half, and it is what makes the answer-time half mean "since".

    Refused before the broker is touched, so this needs no Temporal: a question nobody could answer
    usefully should not become a durable wait somebody has to expire.
    """
    from chemclaw.agent.pending_tools import request_external_input

    _corpus(tmp_path, monkeypatch, _retired())
    monkeypatch.setattr(settings, "entra_required", False)

    async def _run() -> None:
        with pytest.raises(ChemclawError, match="no longer holds"):
            await request_external_input(
                subject="re-run the degassing from [[playbook-degassing]]",
                rationale="the campaign is suspended on it",
            )

    asyncio.run(_run())


def test_the_premise_is_derived_from_what_the_question_cites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never an argument, so the model cannot disable the check by omitting it.

    A `premise_note_ids` parameter would be a control whose producer is a model remembering to
    populate it — the shape this repository has deleted twice as "a claim that a check exists".
    """
    import inspect

    from chemclaw.agent.pending_tools import request_external_input
    from chemclaw.durable.awaiting import AwaitRequest

    assert "premise_note_ids" in AwaitRequest.model_fields, (
        "the wait must carry the premise, or there is nothing to check an answer against"
    )
    declared = set(inspect.signature(request_external_input).parameters)
    assert "premise_note_ids" not in declared, (
        "the premise must be derived from the question's own citations, never accepted as an "
        "argument the model can omit"
    )


def test_the_store_round_trips_the_premise(tmp_path: Path) -> None:
    """The column, the upsert's tuple and the read projection are positional and move together.

    `_row` maps `_COLUMNS` by index, so a column added to one and not the other does not fail — it
    silently mis-assigns every field after it. This is the cheapest assertion that catches that.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clear("premise-roundtrip")
        await pending_store.open_request(
            request_id="premise-roundtrip",
            kind="approval",
            subject="approve the run",
            rationale="it cites [[playbook-degassing]]",
            asked_of="",
            requested_by="u-asker",
            session_id="s-1",
            correlation_id="c-1",
            due_at=datetime.now(UTC) + timedelta(days=7),
            premise_note_ids=["playbook-degassing"],
        )

        stored = await pending_store.get_request("premise-roundtrip")
        assert stored is not None
        assert stored.premise_note_ids == ["playbook-degassing"]
        assert stored.subject == "approve the run", "the projection shifted by a column"

    asyncio.run(_run())


def test_a_re_ask_replaces_the_premise_rather_than_keeping_the_old_one(tmp_path: Path) -> None:
    """A re-ask is a new question, validated against today's corpus.

    Keeping the previous cycle's premise would check an answer against notes this question never
    rested on — and where the old cycle cited a note that has since been retired, would refuse every
    answer to a question whose own premise is whole.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clear("premise-reask")
        for premise, run in (["old-note"], "run-1"), (["new-note"], "run-2"):
            await pending_store.open_request(
                request_id="premise-reask",
                kind="measurement",
                subject="run it",
                rationale="",
                asked_of="",
                requested_by="u-asker",
                session_id="s-1",
                correlation_id="c-1",
                due_at=datetime.now(UTC) + timedelta(days=7),
                premise_note_ids=premise,
                run_id=run,
            )

        stored = await pending_store.get_request("premise-reask")
        assert stored is not None
        assert stored.premise_note_ids == ["new-note"]

    asyncio.run(_run())


def test_an_answer_is_refused_once_its_premise_has_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point, end to end: the wait opened whole and the answer arrives after the change.

    The request is left **waiting**, deliberately. The premise moving is not an ending — somebody
    who re-reads the current evidence can still answer, and the deadline still expires on its own.
    Settling it here would destroy a question nobody decided.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clear("premise-moved")
        await pending_store.open_request(
            request_id="premise-moved",
            kind="measurement",
            subject="re-run the degassing",
            rationale="",
            asked_of="",
            requested_by="u-someone-else",
            session_id="s-1",
            correlation_id="c-1",
            due_at=datetime.now(UTC) + timedelta(days=7),
            premise_note_ids=["playbook-degassing"],
        )

    asyncio.run(_run())

    # The corpus moves while the question waits: the note it rests on is retired.
    _corpus(tmp_path, monkeypatch, _retired())

    app = create_app(connector_factory=_no_connectors, graph_factory=_no_graph)
    app.dependency_overrides[require_principal] = lambda: _ALICE
    with TestClient(app) as client:
        answered = client.post("/pending/premise-moved/answer", json={"payload": {"yield": 0.7}})

    assert answered.status_code == 409
    assert "changed since it was asked" in answered.json()["detail"]
    assert "playbook-degassing" in answered.json()["detail"], "name the note that moved"

    async def _still_open() -> None:
        stored = await pending_store.get_request("premise-moved")
        assert stored is not None
        assert stored.state == "waiting", "a moved premise is not an ending"

    asyncio.run(_still_open())


def test_an_answer_goes_through_while_its_premise_stands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard against a check that refuses everything.

    Without this, a `premise_breaks` that returned a break unconditionally would pass every other
    test in this file. The 503 is the *broker* being absent in this test environment, which is
    exactly the point: the request got past the premise check and died at the signal.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clear("premise-stands")
        await pending_store.open_request(
            request_id="premise-stands",
            kind="measurement",
            subject="re-run the degassing",
            rationale="",
            asked_of="",
            requested_by="u-someone-else",
            session_id="s-1",
            correlation_id="c-1",
            due_at=datetime.now(UTC) + timedelta(days=7),
            premise_note_ids=["playbook-degassing"],
        )

    asyncio.run(_run())
    _corpus(tmp_path, monkeypatch, _standing())

    app = create_app(connector_factory=_no_connectors, graph_factory=_no_graph)
    app.dependency_overrides[require_principal] = lambda: _ALICE
    with TestClient(app) as client:
        answered = client.post("/pending/premise-stands/answer", json={"payload": {}})

    assert answered.status_code != 409, "a standing premise must not refuse the answer"


async def _clear(request_id: str) -> None:
    """Drop this test's own row, so a re-run starts from nothing."""
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("DELETE FROM pending_requests WHERE request_id = %s", (request_id,))
        await conn.execute(
            "DELETE FROM pending_request_answers WHERE request_id = %s", (request_id,)
        )
        await conn.commit()
