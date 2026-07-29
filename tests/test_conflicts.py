"""Disagreeing notes are flagged, never silently both returned (KM-8, S5).

Retrieval used to hand back two contradictory notes with no marker. For a system whose output a
chemist acts on that is worse than returning neither: two notes saying different things read as
corroboration.

The line these tests hold is that a conflict is a *flag*, never a filter. Dropping one side would
be this layer deciding which of two curated notes is right, and it has no basis for that.
"""

from datetime import date

import pytest

from chemclaw.core.config import settings
from chemclaw.kg.conflicts import Conflict, conflicts_by_note, find_conflicts
from chemclaw.kg.note import Note, Relation
from chemclaw.memory.failure import failure_note


def _note(note_id: str, **kwargs: object) -> Note:
    """A minimal note with overridable fields."""
    return Note(id=note_id, type=kwargs.pop("type", "reaction"), **kwargs)  # type: ignore[arg-type]


def test_a_declared_contradiction_is_found() -> None:
    """The unambiguous case: an author said so, and nothing is inferred."""
    left = _note("a", relations=[Relation(rel="contradicts", to="b")])
    conflicts = find_conflicts([left, _note("b")])
    assert [(c.kind, c.note_id, c.other_id) for c in conflicts] == [("declared", "a", "b")]


def test_a_contradiction_of_a_note_that_is_not_in_the_corpus_is_not_reported() -> None:
    """A conflict a reader cannot inspect both halves of is noise, not information."""
    left = _note("a", relations=[Relation(rel="contradicts", to="nowhere")])
    assert find_conflicts([left]) == []


def test_superseded_by_is_not_a_conflict() -> None:
    """A retired note is already out of current-evidence sweeps, so flagging it says nothing.

    `supersedes` *is* a conflict — the surviving note asserts the other is out of date, and a
    reader holding the old one should know. `superseded-by` points the other way, from a note that
    retrieval has already excluded.
    """
    retired = _note("old", relations=[Relation(rel="superseded-by", to="new")])
    assert find_conflicts([retired, _note("new")]) == []

    current = _note("new2", relations=[Relation(rel="supersedes", to="old2")])
    assert len(find_conflicts([current, _note("old2")])) == 1


def test_a_wide_confidence_gap_on_one_compound_is_suspected() -> None:
    """The heuristic, and its deliberately weaker claim.

    It does not say these notes conflict. It says they are the kind of pair worth a reader's eye —
    same type, same molecule, both valid now, one confident and one hedging.
    """
    confident = _note("a", compound_smiles="CCO", confidence=0.95)
    hedging = _note("b", compound_smiles="CCO", confidence=0.4)
    conflicts = find_conflicts([confident, hedging])
    assert [c.kind for c in conflicts] == ["suspected"]
    assert "0.95" in conflicts[0].detail and "0.4" in conflicts[0].detail


def test_a_narrow_confidence_gap_is_not_reported() -> None:
    """Two notes that broadly agree are not a finding; flagging them would make the flag noise."""
    assert (
        find_conflicts(
            [
                _note("a", compound_smiles="CCO", confidence=0.8),
                _note("b", compound_smiles="CCO", confidence=0.75),
            ]
        )
        == []
    )


def test_the_gap_threshold_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set it low and every ordinary pair trips; set it high and only extremes do."""
    notes = [
        _note("a", compound_smiles="CCO", confidence=0.8),
        _note("b", compound_smiles="CCO", confidence=0.6),
    ]
    assert find_conflicts(notes) == []
    monkeypatch.setattr(settings, "conflict_confidence_gap", 0.1)
    assert len(find_conflicts(notes)) == 1


def test_notes_of_different_types_about_one_compound_are_not_compared() -> None:
    """A computed prediction and a measured result are not the same claim.

    Comparing their confidences would flag every molecule the system both computed and measured,
    which is the intended workflow rather than a disagreement.
    """
    assert (
        find_conflicts(
            [
                _note("a", type="job-result", compound_smiles="CCO", confidence=0.6),
                _note("b", type="reaction", compound_smiles="CCO", confidence=0.95),
            ]
        )
        == []
    )


def test_notes_that_were_never_simultaneously_valid_are_a_history_not_a_conflict() -> None:
    """One replaced the other. That is the bi-temporal machinery working, not a disagreement."""
    old = _note(
        "a",
        compound_smiles="CCO",
        confidence=0.9,
        valid_from=date(2024, 1, 1),
        valid_to=date(2024, 12, 31),
    )
    new = _note("b", compound_smiles="CCO", confidence=0.4, valid_from=date(2025, 1, 1))
    assert find_conflicts([old, new]) == []


def test_a_conflict_found_from_both_ends_is_reported_once() -> None:
    """Two notes each declaring they contradict the other is one disagreement."""
    left = _note("a", relations=[Relation(rel="contradicts", to="b")])
    right = _note("b", relations=[Relation(rel="contradicts", to="a")])
    assert len(find_conflicts([left, right])) == 1


def test_either_end_of_a_conflict_can_find_the_pair() -> None:
    """Indexed under both ids — the half a reader is holding is the half that must be flagged."""
    index = conflicts_by_note([Conflict(note_id="a", other_id="b", kind="declared", detail="d")])
    assert set(index) == {"a", "b"}


def test_a_non_current_note_is_out_of_a_retrieval_time_scan() -> None:
    """A retrieval-time scan skips a note retrieval would never have shown.

    `as_of` matches what retrieval sees, so an expired note is not reported as conflicting with its
    own replacement — the reader was never going to be handed it.
    """
    expired = _note(
        "old",
        valid_to=date(2020, 1, 1),
        relations=[Relation(rel="contradicts", to="new")],
    )
    notes = [expired, _note("new")]
    assert len(find_conflicts(notes)) == 1  # a curation pass over the whole corpus sees it
    assert find_conflicts(notes, as_of=date.today()) == []  # a retrieval-time scan does not


def test_a_failure_note_contradicts_what_it_refutes_and_is_therefore_findable() -> None:
    """The negative-feedback loop closing (KM-12).

    Before typed edges a correction could only be prose, so `find_conflicts` could not see it and a
    later query served the refuted note with no indication anything was wrong. The `contradicts`
    relation is what makes the feedback actually feed back.
    """
    playbook = _note("playbook-x", type="playbook")
    reported = failure_note(
        "playbook-x",
        "Ran it four times at scale; the yield was half what the playbook claims.",
        reported_by="chemist@example.com",
        confidence=0.7,
    )
    assert reported.type == "failure-mode"
    assert reported.created_by == "agent"  # goes through the PR-gate like everything else
    conflicts = find_conflicts([playbook, reported])
    assert [(c.kind, c.other_id) for c in conflicts] == [("declared", "playbook-x")]


def test_reporting_the_same_failure_twice_is_idempotent() -> None:
    """Two people hitting the same problem should not produce two records of it."""
    args = (
        "playbook-x",
        "the yield was half",
    )
    first = failure_note(*args, reported_by="a@example.com")
    second = failure_note(*args, reported_by="b@example.com")
    assert first.id == second.id


def test_a_different_observation_about_one_note_is_its_own_record() -> None:
    """Two people hitting two different problems with one playbook is two findings, not one."""
    first = failure_note("playbook-x", "the yield was half", reported_by="a@example.com")
    second = failure_note("playbook-x", "it did not dissolve at all", reported_by="a@example.com")
    assert first.id != second.id


def test_a_failure_note_is_not_current_before_it_was_observed() -> None:
    """A correction is not retroactively true for a period it says nothing about."""
    reported = failure_note(
        "playbook-x", "did not hold", reported_by="a@example.com", as_of=date(2026, 3, 1)
    )
    assert reported.valid_from == date(2026, 3, 1)
    assert not reported.is_current(date(2026, 1, 1))
    assert reported.is_current(date(2026, 6, 1))
