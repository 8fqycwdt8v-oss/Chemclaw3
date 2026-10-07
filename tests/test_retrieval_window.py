"""A date-windowed sweep judges currency as of the requested window, not today.

Applying today's date dropped retired notes the window explicitly asked about, which looks like
an empty period. An unwindowed sweep still drops retired notes. `since`/`until` window a note by
when its subject happened; they are not an "as of" filter.
"""

import asyncio
from datetime import date
from pathlib import Path

from chemclaw.kg.graph import invalidate_cache
from chemclaw.kg.note import Note
from chemclaw.kg.render import render_note
from chemclaw.retrieval.retrievers import GraphRetriever, _eligible_sync

_WINDOW = {"since": date(2024, 6, 1), "until": date(2025, 1, 1)}


def _corpus(directory: Path, *notes: Note) -> str:
    """Write notes into a fresh knowledge tree and return its root path."""
    root = directory / "knowledge"
    for note in notes:
        path = root / note.type / f"{note.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_note(note), encoding="utf-8")
    invalidate_cache()
    return str(root)


def _retired() -> Note:
    """A note that was current inside `_WINDOW` and is retired today."""
    return Note(
        id="playbook-degassing-2024",
        type="playbook",
        body="Sparge for twenty minutes per 100 mL before adding the catalyst.",
        valid_from=date(2024, 7, 1),
        valid_to=date(2024, 12, 31),
    )


def test_a_note_that_was_current_in_the_requested_window_is_served_for_that_window(
    tmp_path: Path,
) -> None:
    """The measured defect: zero chunks from every leg, indistinguishable from an empty period."""
    root = _corpus(tmp_path, _retired())

    chunks = asyncio.run(GraphRetriever(root).retrieve("degassing sparge catalyst", _WINDOW))

    assert [chunk.source_note_id for chunk in chunks] == ["playbook-degassing-2024"]


def test_the_same_note_stays_out_of_an_unwindowed_current_evidence_sweep(tmp_path: Path) -> None:
    """A retired note stays out of an unwindowed current-evidence sweep; it is still reachable by
    id.
    """
    root = _corpus(tmp_path, _retired())

    chunks = asyncio.run(GraphRetriever(root).retrieve("degassing sparge catalyst", {}))

    assert chunks == []


def test_the_window_is_the_only_date_rule_a_windowed_sweep_applies(tmp_path: Path) -> None:
    """The window is the only date rule a windowed sweep applies; it is not switched off.

    A note with `valid_from` outside the period is still excluded. Any admitted note was current
    somewhere in the window, which is asserted so loosening `_in_window` fails here.
    """
    root = _corpus(
        tmp_path,
        _retired(),
        Note(
            id="playbook-degassing-2026",
            type="playbook",
            body="Sparge for ninety seconds before adding the catalyst.",
            valid_from=date(2026, 1, 1),
        ),
    )

    eligible = _eligible_sync(Path(root), _WINDOW, date.today())

    assert set(eligible) == {"playbook-degassing-2024"}


def test_a_note_the_window_admits_was_current_somewhere_inside_it(tmp_path: Path) -> None:
    """The implication the loop relies on instead of restating it as a predicate."""
    notes = [
        Note(
            id=f"playbook-{index}",
            type="playbook",
            body="Degas before adding the catalyst.",
            valid_from=valid_from,
            valid_to=valid_to,
        )
        for index, (valid_from, valid_to) in enumerate(
            [
                (date(2024, 7, 1), date(2024, 12, 31)),
                (date(2024, 7, 1), None),
                (date(2024, 6, 1), date(2024, 6, 1)),
                (date(2025, 1, 1), date(2030, 1, 1)),
            ]
        )
    ]
    root = _corpus(tmp_path, *notes)

    eligible = _eligible_sync(Path(root), _WINDOW, date.today())

    assert len(eligible) == len(notes)
    for note in eligible.values():
        assert note.valid_from is not None
        assert note.is_current(note.valid_from), note.id
        assert _WINDOW["since"] <= note.valid_from <= _WINDOW["until"], note.id


def _mutually_contradictory(retired: bool) -> list[Note]:
    """Two playbooks that declare `[[contradicts:]]` on each other, retired in the window or open.

    Only the validity window differs between the two, so the comparison isolates the date rule.
    """
    closed = date(2024, 12, 31) if retired else None
    return [
        Note(
            id="playbook-sparge-a",
            type="playbook",
            body="Sparge for twenty minutes per 100 mL before adding the catalyst.",
            valid_from=date(2024, 7, 1),
            valid_to=closed,
        ),
        Note(
            id="playbook-sparge-b",
            type="playbook",
            body="Do not sparge: add the catalyst under argon. [[contradicts:playbook-sparge-a]]",
            valid_from=date(2024, 8, 1),
            valid_to=closed,
        ),
        Note(
            id="failure-sparge",
            type="failure-mode",
            body="Sparging twenty minutes stalled the catalyst. [[refutes:playbook-sparge-a]]",
            valid_from=date(2024, 9, 1),
            valid_to=closed,
        ),
    ]


def test_a_windowed_sweep_flags_the_contradictions_among_the_notes_it_serves(
    tmp_path: Path,
) -> None:
    """A windowed sweep flags the contradictions among the notes it serves.

    The conflict index must use the same window as eligibility; scanning as of today returns no
    conflicts for notes retired inside the window, and `until` alone misses exactly those.
    """
    root = _corpus(tmp_path, *_mutually_contradictory(retired=True))

    chunks = asyncio.run(GraphRetriever(root).retrieve("sparge catalyst", _WINDOW))

    flagged = {chunk.source_note_id: sorted(chunk.conflicts_with) for chunk in chunks}
    assert flagged.get("playbook-sparge-a") == ["playbook-sparge-b"], flagged
    assert flagged.get("playbook-sparge-b") == ["playbook-sparge-a"], flagged
    assert all(chunk.conflicts_total >= len(chunk.conflicts_with) for chunk in chunks)


def test_the_unwindowed_sweep_still_judges_conflicts_as_of_today(tmp_path: Path) -> None:
    """The unwindowed sweep still judges conflicts as of today; the control for the test above."""
    root = _corpus(tmp_path, *_mutually_contradictory(retired=False))

    chunks = asyncio.run(GraphRetriever(root).retrieve("sparge catalyst", {}))

    flagged = {chunk.source_note_id: sorted(chunk.conflicts_with) for chunk in chunks}
    assert flagged.get("playbook-sparge-a") == ["playbook-sparge-b"], flagged
    assert flagged.get("playbook-sparge-b") == ["playbook-sparge-a"], flagged

    retired = _corpus(tmp_path / "retired", *_mutually_contradictory(retired=True))
    assert asyncio.run(GraphRetriever(retired).retrieve("sparge catalyst", {})) == []

    # A live note contradicted by a retired one: unwindowed, the retired note is unseen, so its
    # dispute is not flagged. Without this, scanning the whole corpus always would pass.
    mixed = _corpus(
        tmp_path / "mixed",
        Note(
            id="playbook-sparge-a",
            type="playbook",
            body="Sparge for twenty minutes per 100 mL before adding the catalyst.",
            valid_from=date(2024, 7, 1),
        ),
        Note(
            id="playbook-sparge-old",
            type="playbook",
            body="Do not sparge: add the catalyst dry. [[contradicts:playbook-sparge-a]]",
            valid_from=date(2023, 1, 1),
            valid_to=date(2023, 12, 31),
        ),
    )
    live = asyncio.run(GraphRetriever(mixed).retrieve("sparge catalyst", {}))
    assert [chunk.source_note_id for chunk in live] == ["playbook-sparge-a"]
    assert live[0].conflicts_with == [], (
        "a dispute raised only by a note this sweep cannot serve is not a flag a reader can act on"
    )


def test_a_chunk_from_a_retired_note_says_when_it_stopped_being_valid(tmp_path: Path) -> None:
    """A chunk from a retired note says when it stopped being valid.

    Windowed sweeps serve retired notes on purpose, and a note can be retired with nothing
    contradicting it.
    """
    root = _corpus(tmp_path, _retired())

    chunks = asyncio.run(GraphRetriever(root).retrieve("degassing sparge catalyst", _WINDOW))

    assert [chunk.valid_to for chunk in chunks] == [date(2024, 12, 31)]

    live = _corpus(
        tmp_path / "live",
        Note(
            id="playbook-degassing-2024",
            type="playbook",
            body="Sparge for twenty minutes per 100 mL before adding the catalyst.",
            valid_from=date(2024, 7, 1),
        ),
    )
    open_chunks = asyncio.run(GraphRetriever(live).retrieve("degassing sparge catalyst", _WINDOW))
    assert [chunk.valid_to for chunk in open_chunks] == [None], (
        "an open validity window is `None`, not a sentinel date a renderer would print"
    )
