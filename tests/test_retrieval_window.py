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
