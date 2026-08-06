"""Disagreeing notes are flagged, never silently both returned (KM-8, S5).

Retrieval used to hand back two contradictory notes with no marker. For a system whose output a
chemist acts on that is worse than returning neither: two notes saying different things read as
corroboration.

The line these tests hold is that a conflict is a *flag*, never a filter. Dropping one side would
be this layer deciding which of two curated notes is right, and it has no basis for that.
"""

from datetime import date
from pathlib import Path

import pytest

import chemclaw.kg.conflicts as conflicts_module
import chemclaw.kg.graph as graph
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.kg.conflicts import Conflict, conflicts_by_note, find_conflicts
from chemclaw.kg.note import Note, Relation
from chemclaw.kg.relations import KNOWN_RELATIONS
from chemclaw.memory.failure import close_refuted_note, failure_note


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


def test_retiring_the_refuted_note_ends_the_flag_and_keeps_the_history() -> None:
    """Why `close_refuted_note` is opt-in, measured rather than argued.

    Closing the refuted note is the right move for a claim that *stopped* being true and the wrong
    one for a claim that never was, and the difference is observable in two places at once:

    - the note keeps answering `is_current` **True** inside its old window, i.e. `valid_to` states
      that the claim did hold up to that date — a fresh false statement about a never-true claim;
    - the retrieval-time conflict scan then reports nothing, because a note nothing serves needs no
      flag. Leave it open and the disagreement is on every retrieval instead.
    """
    claim = _note("playbook-x", type="playbook", valid_from=date(2024, 1, 1))
    reported = failure_note("playbook-x", "half the yield", reported_by="a@example.com")
    today = date.today()

    assert len(find_conflicts([claim, reported], as_of=today)) == 1

    retired = close_refuted_note(claim, reported.id, date(2025, 1, 1))
    assert find_conflicts([retired, reported], as_of=today) == []
    assert retired.is_current(date(2024, 6, 1))  # the claim's own history is preserved…
    assert not retired.is_current(today)  # …and it is out of current evidence
    assert reported.id in retired.outgoing_links()  # the old note points at what ended it


def test_a_backwards_retirement_window_is_refused_with_both_dates() -> None:
    """The schema rejects `valid_to < valid_from`; saying so beats a `ValidationError`."""
    claim = _note("playbook-x", type="playbook", valid_from=date(2026, 5, 1))
    with pytest.raises(ChemclawError, match="only became valid on 2026-05-01"):
        close_refuted_note(claim, "failure-abc", date(2026, 3, 1))


def _write(directory: Path, note_id: str, *, smiles: str, confidence: float) -> None:
    """Write a minimal reaction note that the suspected-conflict heuristic can pair up."""
    (directory / f"{note_id}.md").write_text(
        f"---\nid: {note_id}\ntype: reaction\ncompound_smiles: {smiles}\n"
        f"confidence: {confidence}\n---\nbody\n",
        encoding="utf-8",
    )


def test_the_conflict_index_is_computed_once_per_corpus_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The derived map is cached behind the notes fingerprint, like the notes and the graph.

    It was the one whole-corpus artifact that was not, and it is the expensive one: every
    `SourceRetriever.retrieve` recomputed it, so a `gather_evidence` sweep paid it once per enabled
    note-backed source and the next sweep over an unchanged corpus paid the lot again. Measured on
    a 2,000-note corpus shaped like a real programme (many runs on a few substrates), a three-source
    sweep went from 2,458 ms to 22 ms once the answer was reused.

    A changed corpus must still bust it, which is the half that makes the cache safe: a conflict
    nobody is shown because the flag is stale is exactly the failure KM-8 exists to prevent.
    """
    monkeypatch.setattr(settings, "graph_cache_enabled", True)
    monkeypatch.setattr(settings, "graph_cache_ttl_seconds", 0.0)
    monkeypatch.setattr(settings, "conflict_detection_enabled", True)
    conflicts_module._INDEX_CACHE.clear()
    graph._NOTES_CACHE.clear()
    graph._LAST_SCAN.clear()
    scans = {"count": 0}
    real_find = conflicts_module.find_conflicts

    def _counting(notes: list[Note], as_of: date | None = None) -> list[Conflict]:
        scans["count"] += 1
        return real_find(notes, as_of=as_of)

    monkeypatch.setattr(conflicts_module, "find_conflicts", _counting)
    _write(tmp_path, "a", smiles="CCO", confidence=0.95)
    _write(tmp_path, "b", smiles="CCO", confidence=0.2)
    today = date.today()

    first = conflicts_module.conflict_index(tmp_path, today)
    second = conflicts_module.conflict_index(tmp_path, today)
    assert scans["count"] == 1, "a second sweep over an unchanged corpus must reuse the answer"
    assert first == second
    assert {note_id: flags.ids for note_id, flags in first.items()} == {"a": ["b"], "b": ["a"]}

    _write(tmp_path, "c", smiles="CCO", confidence=0.9)
    third = conflicts_module.conflict_index(tmp_path, today)
    assert scans["count"] == 2, "a changed corpus must bust it — a stale flag is worse than none"
    assert sorted(third["b"].ids) == ["a", "c"]


def test_the_conflict_index_is_recomputed_for_a_different_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`as_of` is part of the key: yesterday's map is a different answer, not a stale one.

    `find_conflicts` scans only the notes current on the day it is asked about, so a cache keyed on
    the corpus alone would serve a long-running process the verdict it computed the first time it
    was asked — and a note whose validity window closed overnight would keep being flagged.
    """
    monkeypatch.setattr(settings, "graph_cache_enabled", True)
    monkeypatch.setattr(settings, "graph_cache_ttl_seconds", 0.0)
    monkeypatch.setattr(settings, "conflict_detection_enabled", True)
    conflicts_module._INDEX_CACHE.clear()
    (tmp_path / "a.md").write_text(
        "---\nid: a\ntype: reaction\ncompound_smiles: CCO\nconfidence: 0.95\n"
        "valid_to: 2026-01-31\n---\nbody\n",
        encoding="utf-8",
    )
    _write(tmp_path, "b", smiles="CCO", confidence=0.2)

    while_valid = conflicts_module.conflict_index(tmp_path, date(2026, 1, 15))
    after_expiry = conflicts_module.conflict_index(tmp_path, date(2026, 2, 15))
    assert {note_id: flags.ids for note_id, flags in while_valid.items()} == {
        "a": ["b"],
        "b": ["a"],
    }
    assert after_expiry == {}, "a retired note is out of current evidence, so it flags nothing"


def test_the_conflict_index_is_empty_when_detection_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off means no flags at all — the caller cannot tell "no conflicts" from "not looking"."""
    monkeypatch.setattr(settings, "conflict_detection_enabled", False)
    _write(tmp_path, "a", smiles="CCO", confidence=0.95)
    _write(tmp_path, "b", smiles="CCO", confidence=0.2)
    assert conflicts_module.conflict_index(tmp_path, date.today()) == {}


def test_the_conflict_index_of_an_absent_directory_is_empty() -> None:
    """A deployment with no note tree yet asks the same question and gets a usable answer."""
    assert conflicts_module.conflict_index(Path("/nonexistent-knowledge-dir"), date.today()) == {}


def test_the_conflicting_relations_are_all_real_relations() -> None:
    """A relation named here but absent from the vocabulary detects nothing, and says nothing.

    `_CONFLICTING_RELATIONS` is matched against edges whose relation `kg-validate` has already
    forced into `KNOWN_RELATIONS`; a name outside it can therefore never match, so the detector
    would quietly stop finding declared conflicts while every test using the other member kept
    passing.
    """
    assert conflicts_module._CONFLICTING_RELATIONS <= KNOWN_RELATIONS


def _crowd(count: int, *, confidence_of: object = None) -> list[Note]:
    """`count` notes on one substrate whose confidences span the whole range.

    The shape a real programme has, and the one the exhaustive scan choked on: an optimization
    campaign is many runs on one substrate, so every note pairs with every other.
    """
    return [
        _note(f"n{i:03d}", compound_smiles="CCO", confidence=round(0.02 + i * (0.96 / count), 2))
        for i in range(count)
    ]


def test_a_note_is_flagged_against_its_widest_disagreements_not_against_everything() -> None:
    """The narrowing, stated as the claim it changes rather than as a number.

    An exhaustive pairwise scan tells a reader "here is every note on this substrate whose
    confidence differs from yours" — a fact about the corpus, not a signal about the note. On a
    programme-shaped 2,000-note corpus over 7 substrates that was 141,156 pairs and ~141 ids on
    every evidence chunk reaching the model. KM-8 asked for a signal; the strongest few are one.
    """
    notes = _crowd(40)
    by_note = conflicts_by_note(find_conflicts(notes))
    # The most confident note disagrees with every hedging one, so it is the worst case.
    widest = conflicts_module._strongest("n039", by_note["n039"])
    assert len(widest.ids) == settings.conflict_max_per_note
    assert widest.ids == ["n000", "n001", "n002"], "the widest gaps, worst first"
    assert widest.total > len(widest.ids), "and there were more"


def test_the_flag_carries_how_many_were_left_out() -> None:
    """A truncated list with nothing saying so reads as a complete one — the repo's standing rule.

    Without the count a reader shown three ids concludes there were three, which is a stronger and
    wronger statement than the exhaustive list ever made.
    """
    by_note = conflicts_by_note(find_conflicts(_crowd(40)))
    flags = conflicts_module._strongest("n039", by_note["n039"])
    assert flags.truncated == flags.total - len(flags.ids) > 0
    stated = [_note("a", relations=[Relation(rel="contradicts", to="b")]), _note("b")]
    complete = conflicts_module._strongest("a", conflicts_by_note(find_conflicts(stated))["a"])
    assert complete.truncated == 0, "an untruncated flag must not claim a hidden remainder"


def test_a_declared_conflict_outranks_a_suspected_one_for_the_places() -> None:
    """`Conflict.kind` is load-bearing: an author's statement is not evicted by a heuristic's guess.

    This is the property that lets the cap apply at all. Ranking by gap alone would let three
    hedging notes push a stated contradiction off a note's flag entirely.
    """
    notes = _crowd(40)
    notes[39] = _note(
        "n039",
        compound_smiles="CCO",
        confidence=0.98,
        relations=[Relation(rel="contradicts", to="n020")],
    )
    by_note = conflicts_by_note(find_conflicts(notes))
    flags = conflicts_module._strongest("n039", by_note["n039"])
    assert flags.ids[0] == "n020", "the stated contradiction, ahead of every wider confidence gap"


def test_the_scan_stops_at_the_threshold_rather_than_enumerating_every_pair() -> None:
    """The cost and the noise are the same fact, so fixing one must fix the other.

    Measured on the corpus shape the finding named: 141,156 pairs and 637 ms before, and the walk
    that takes each note's widest disagreements first can stop as soon as the wider end falls under
    the threshold. The assertion is against the pair count rather than a wall-clock number, which
    is the part that is a property of the algorithm rather than of the machine.
    """
    notes = [
        _note(
            f"n{i:04d}",
            compound_smiles=f"C{'C' * (i % 7)}O",
            confidence=round(0.05 + (i % 20) * 0.05, 2),
        )
        for i in range(2000)
    ]
    found = find_conflicts(notes)
    assert len(found) < 10_000, (
        f"{len(found)} pairs — the exhaustive scan produced 141,156 on this corpus, and a list "
        "that long is a fact about the corpus rather than a signal about any note"
    )


def test_notes_that_all_agree_cost_nothing_and_flag_nothing() -> None:
    """The early stop must not invent a disagreement where the whole group is within the gap."""
    same = [_note(f"s{i}", compound_smiles="CCO", confidence=0.8) for i in range(50)]
    assert find_conflicts(same) == []


def test_the_walk_stops_at_the_first_agreeing_candidate_instead_of_reading_the_whole_group() -> (
    None
):
    """The early stop is the half that makes the scan cheap, and it is invisible in the output.

    Bounding what each note *emits* already bounds the flags a reader sees; it does not bound the
    work, because a group of 500 notes that all agree still has 500 candidates to reject. The walk
    takes the widest-disagreeing end first, so the moment that end is inside the threshold nothing
    further in can beat it and the rest of the group need never be read — which is why replacing
    the `break` with a `continue` changes no assertion about conflicts at all.

    So this asserts the reads rather than the results, by handing the walk a sequence that counts
    them. Two per note (the two ends) is the whole cost of a group that agrees.
    """

    class _Counting(list):  # type: ignore[type-arg]
        reads = 0

        def __getitem__(self, index):  # type: ignore[no-untyped-def]
            type(self).reads += 1
            return super().__getitem__(index)

    ordered = _Counting(
        (_note(f"s{i}", compound_smiles="CCO", confidence=0.8), 0.8) for i in range(500)
    )
    _Counting.reads = 0
    assert conflicts_module._widest_disagreements(ordered, 0, 0.3, 3) == []
    assert _Counting.reads <= 6, (
        f"read {_Counting.reads} of 500 candidates to reject a group that agrees — the walk must "
        "stop at the first end inside the threshold, not scan past it"
    )
