"""`WritableNoteStore.put` means the same thing in both stores.

Each test runs once per store (`STORES`): the git store writes through the real git writer on a real
repository, the Postgres store through its transaction. What differs on purpose (git keeps no
revisions) is asserted for the store it belongs to.
"""

from datetime import date
from pathlib import Path

import pytest

from chemclaw.kg.note import Note, Relation
from chemclaw.kg.store import NoteUnit, NoteWriteError, PutOutcome
from tests.note_store_harness import STORES, Harness


@pytest.fixture(params=sorted(STORES))
async def harness(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Harness:
    """An empty store of each kind."""
    return await STORES[request.param](tmp_path, monkeypatch)


def _note(note_id: str, body: str = "A note.", **fields: object) -> Note:
    return Note.model_validate(
        {"id": note_id, "type": "playbook", "created_by": "agent", "body": body, **fields}
    )


async def test_a_put_reports_the_subject_it_wrote_and_a_repeat_writes_nothing(
    harness: Harness,
) -> None:
    first = await harness.store.put(NoteUnit(_note("subject", "See [[other]].")))
    assert isinstance(first, PutOutcome) and first.subject == "subject"
    assert first.written == ("subject",) and first.notes == 1
    again = await harness.store.put(NoteUnit(_note("subject", "See [[other]].")))
    assert again.written == () and again.notes == 0
    edited = await harness.store.put(NoteUnit(_note("subject", "See [[other]], edited.")))
    assert edited.written == ("subject",)
    stored = await harness.store.get("subject")
    assert stored is not None and stored.body == "See [[other]], edited."


async def test_a_dependency_is_written_when_absent_and_never_replaced(harness: Harness) -> None:
    await harness.store.put(NoteUnit(_note("subject", "Uses [[dep]]."), (_note("dep", "Made."),)))
    await harness.add(_note("dep", "Edited by someone."))
    outcome = await harness.store.put(
        NoteUnit(_note("subject", "Uses [[dep]]."), (_note("dep", "Made."),))
    )
    assert outcome.written == ()
    dependency = await harness.store.get("dep")
    assert dependency is not None and dependency.body == "Edited by someone."


async def test_a_retirement_closes_the_note_and_keeps_the_sentence_that_says_why(
    harness: Harness,
) -> None:
    """Both stores end with the same retired note: its window, its edge and the added sentence."""
    await harness.add(_note("old", "The claim."))
    stored = await harness.store.get("old")
    assert stored is not None
    retired = stored.model_copy(
        update={
            "valid_to": date(2026, 3, 31),
            "relations": [Relation(rel="superseded-by", to="new")],
            "body": f"{stored.body}\n\nSuperseded by [[new]]: kept for the record.",
        }
    )
    await harness.store.put(NoteUnit(_note("new", "Replaces [[old]]."), retirements=(retired,)))
    closed = await harness.store.get("old")
    assert closed is not None
    assert closed.valid_to == date(2026, 3, 31)
    assert closed.body == "The claim.\n\nSuperseded by [[new]]: kept for the record."
    assert await harness.store.related("old", "superseded-by") == ["new"]


async def test_a_retirement_of_a_persons_note_is_left_alone_and_the_subject_lands(
    harness: Harness,
) -> None:
    await harness.add(Note(id="curated", type="playbook", created_by="human", body="A person's."))
    closed = Note(
        id="curated",
        type="playbook",
        created_by="human",
        body="A person's.",
        valid_to=date(2026, 1, 1),
    )
    outcome = await harness.store.put(
        NoteUnit(_note("successor", "Replaces [[curated]]."), retirements=(closed,))
    )
    assert outcome.written == ("successor",)
    kept = await harness.store.get("curated")
    assert kept is not None and kept.valid_to is None


async def test_a_conditional_write_is_a_postgres_feature_and_the_tree_refuses_it(
    harness: Harness,
) -> None:
    unit = NoteUnit(_note("guarded"), expected_revision=0)
    if type(harness.store).__name__ == "GitNoteStore":
        with pytest.raises(NoteWriteError, match="keeps no revisions"):
            await harness.store.put(unit)
        assert (await harness.store.put(NoteUnit(_note("guarded")))).revisions == {}
    else:
        assert (await harness.store.put(unit)).revisions == {"guarded": 1}
