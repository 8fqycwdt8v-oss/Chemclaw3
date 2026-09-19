"""A date-windowed sweep applied two date rules, and the caller asked for one (W14 finding D).

`_eligible_sync` gated on `note.is_current(today)` *before* `_in_window(note, since, until)`. So a
sweep windowed on 2024 admitted a note by the rule the caller gave — its `valid_from` falls inside
the window — and then removed it by a rule the caller did not give: it is not current *now*. The
two silences compose into an honest-looking absence, because a windowed query that returns nothing
is shape-identical to a period with nothing in it.

Dropping a retired note from a **current-evidence** sweep is correct and stays correct (KM-7,
D-055). What was wrong is applying today's date to a question explicitly about another date;
D-055's own consequence paragraph names `is_current` as "the single seam to branch on" for exactly
this. So currency is judged as of the *requested* window when there is one, and as of today when
there is not.

**What this does not fix, said out loud because the finding that prompted it claimed otherwise.**
"What did we recommend for degassing in 2024?" over the shipped corpus still returns nothing, and
the reason is the *other* gate: `playbook-degassing-old` has `valid_from: 2024-01-01`, so a window
of 2024-06-01..2025-01-01 excludes it on `_in_window` — the rule the caller did give — regardless
of currency. Measured, the whole 39-note corpus is empty under that window for the same reason, so
the zero the finding attributed to the currency check had a second sufficient cause. `since`/
`until` window a note by when its subject *happened* (D-162); they are not an "as of" filter, and
widening them into one would make "what have I tried in the last two weeks" return runs from three
years ago whose validity still overlaps. That is a separate decision, not this one.
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
    """KM-7, unchanged and asserted beside the change so it cannot be traded away.

    A sweep that names no period is asking what is true now, and a retired playbook is not an
    answer to that. It stays in Git and reachable by explicit id (D-055); it is only dropped from
    current-evidence results.
    """
    root = _corpus(tmp_path, _retired())

    chunks = asyncio.run(GraphRetriever(root).retrieve("degassing sparge catalyst", {}))

    assert chunks == []


def test_the_window_is_the_only_date_rule_a_windowed_sweep_applies(tmp_path: Path) -> None:
    """The branch narrows the currency rule to the window; it does not switch the gate off.

    A note whose `valid_from` lies *outside* the requested period is still excluded, by the rule
    the caller gave. That is also why no separate overlap test is written into the loop: for any
    note `_in_window` admits, `since <= valid_from <= until` holds, and `valid_to >= valid_from`
    is a `Note` model invariant — so the note was necessarily current somewhere inside the
    requested period. A second predicate asserting that would be a control with nothing to
    decide. The implication is asserted below instead, so loosening `_in_window` fails here
    rather than quietly re-opening the hole.
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

    The same three notes either way, so the only difference between the two sweeps below is the
    validity window — which is what makes the comparison evidence about the date rule rather than
    about the corpus.
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
    """The currency skip above had a second consumer that was never told about it.

    `_eligible_sync` deliberately does not apply today's date to a windowed sweep, and
    `_conflict_index` asked `conflict_index(directory, date.today())` — and `find_conflicts` scans
    only the notes current at `as_of`. So every note a window admitted *because* it was retired
    came back with `conflicts_with=[]`, which is what a note nothing disagrees with looks like.
    Driven on this corpus: the pair reports one conflict each when left open-ended and zero each
    when retired inside the requested window. Contradiction is one of the three mechanisms
    `D-2026-09-05-the-gate-follows-behaviour-not-knowledge` names as what keeps unreviewed knowledge
    safe, and a flag that cannot fire is not one of them.

    There is no single date that would have done instead — `until` is the obvious candidate and
    misses precisely the notes that retired inside the window, which is this corpus.
    """
    root = _corpus(tmp_path, *_mutually_contradictory(retired=True))

    chunks = asyncio.run(GraphRetriever(root).retrieve("sparge catalyst", _WINDOW))

    flagged = {chunk.source_note_id: sorted(chunk.conflicts_with) for chunk in chunks}
    assert flagged.get("playbook-sparge-a") == ["playbook-sparge-b"], flagged
    assert flagged.get("playbook-sparge-b") == ["playbook-sparge-a"], flagged
    assert all(chunk.conflicts_total >= len(chunk.conflicts_with) for chunk in chunks)


def test_the_unwindowed_sweep_still_judges_conflicts_as_of_today(tmp_path: Path) -> None:
    """The other rule, asserted beside the change so widening the window cannot widen this.

    An unwindowed sweep is asking what is true now, so a retired note is out of the evidence
    altogether and reporting it as a conflict would be noise about a note the reader cannot see. The
    same corpus left open-ended is the control for the test above: this is where the flag was always
    working, and it must keep working on today's date rather than on the whole corpus.
    """
    root = _corpus(tmp_path, *_mutually_contradictory(retired=False))

    chunks = asyncio.run(GraphRetriever(root).retrieve("sparge catalyst", {}))

    flagged = {chunk.source_note_id: sorted(chunk.conflicts_with) for chunk in chunks}
    assert flagged.get("playbook-sparge-a") == ["playbook-sparge-b"], flagged
    assert flagged.get("playbook-sparge-b") == ["playbook-sparge-a"], flagged

    retired = _corpus(tmp_path / "retired", *_mutually_contradictory(retired=True))
    assert asyncio.run(GraphRetriever(retired).retrieve("sparge catalyst", {})) == []

    # And the case that actually separates the two rules: a live note contradicted by a *retired*
    # one. The retired note is not in an unwindowed sweep, so flagging its dispute would point the
    # reader at something they cannot see — which is the noise `conflict_index`'s `as_of` exists to
    # keep out. Without this, "scan the whole corpus always" passes every assertion above.
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
    """The other half of the same gap: the chunk was byte-identical to one from a live note.

    A windowed sweep serves retired notes on purpose, and `EvidenceChunk` carried the note's author,
    source and confidence but nothing about its validity — so the most confident-looking form of
    withdrawn advice reached the model with no signal at all. The conflict flag above is not a
    substitute, because a note can be retired without anything contradicting it.
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
