"""Disagreeing notes are flagged, never silently both returned (KM-8, S5).

Two contradictory notes returned without a marker read as corroboration. A conflict is a flag,
never a filter: this layer has no basis for deciding which curated note is right.
"""

from datetime import date, timedelta
from pathlib import Path

import pytest

import chemclaw.kg.conflicts as conflicts_module
import chemclaw.kg.graph as graph
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.kg.conflicts import Conflict, conflicts_by_note, find_conflicts
from chemclaw.kg.note import Note, Relation
from chemclaw.kg.relations import KNOWN_RELATIONS
from chemclaw.memory.failure import (
    close_refuted_note,
    failure_note,
    failures_against,
    observation_of,
)


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
    """`superseded-by` is not a conflict; `supersedes` is.

    A retired note is already excluded from current-evidence sweeps, so flagging it says nothing.
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
    """A failure note contradicts what it refutes and is therefore findable (KM-12).

    The `contradicts` relation lets `find_conflicts` surface a correction, so a refuted note is
    served with a flag.
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
    """Retiring the refuted note ends the flag and keeps the history.

    `close_refuted_note` is opt-in: closing is right for a claim that stopped being true and wrong
    for one that never was, since the note then answers `is_current` True inside its old window and
    the conflict scan stops reporting it. Left open, the disagreement is flagged on every retrieval.
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
    """The conflict index is computed once per corpus state, cached behind the notes fingerprint.

    Without the cache every `SourceRetriever.retrieve` recomputes it, once per note-backed source. A
    changed corpus must still bust it, or a stale flag hides a conflict.
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

    `find_conflicts` scans notes current on the day asked, so a note whose window closed overnight
    must stop being flagged.
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
    """The conflicting relations are all real relations.

    Edges are validated against `KNOWN_RELATIONS`, so a name outside it can never match and the
    detector would silently miss that kind of conflict.
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
    """A note is flagged against its widest disagreements, not against everything.

    An exhaustive pairwise list is a fact about the corpus, not a signal about the note; the
    strongest few are the signal KM-8 asked for.
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
    """The scan stops at the threshold rather than enumerating every pair.

    Taking each note's widest disagreements first lets the walk stop once the wider end is under the
    threshold. Asserted on the pair count, a property of the algorithm, not on wall clock.
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
    """The walk stops at the first agreeing candidate instead of reading the whole group.

    Bounding emitted flags does not bound work: an agreeing group still has every candidate to
    reject. The early `break` is invisible in the output, so this counts reads: two per note (the
    two ends) for a group that agrees.
    """

    class _Counting(list):  # type: ignore[type-arg]
        reads = 0

        def __getitem__(self, index):  # type: ignore[no-untyped-def]
            type(self).reads += 1
            return super().__getitem__(index)

    ordered = _Counting(
        (_note(f"s{i}", compound_smiles="CCO", confidence=0.8), 0.8) for i in range(500)
    )
    probe, probe_confidence = list.__getitem__(ordered, 0)
    _Counting.reads = 0
    assert conflicts_module._widest_disagreements(ordered, probe, probe_confidence, 0.3, 3) == []
    assert _Counting.reads <= 6, (
        f"read {_Counting.reads} of 500 candidates to reject a group that agrees — the walk must "
        "stop at the first end inside the threshold, not scan past it"
    )


# --- the dated corpus: the window classes and the sweep --------------------------------------


def test_two_notes_with_overlapping_closed_windows_are_suspected() -> None:
    """The sweep's positive case: closed windows that genuinely intersect must still pair."""
    left = _note(
        "a",
        compound_smiles="CCO",
        confidence=0.9,
        valid_from=date(2026, 1, 1),
        valid_to=date(2026, 3, 1),
    )
    right = _note(
        "b",
        compound_smiles="CCO",
        confidence=0.1,
        valid_from=date(2026, 2, 1),
        valid_to=date(2026, 4, 1),
    )
    found = find_conflicts([left, right])
    assert [(c.kind, c.pair()) for c in found] == [("suspected", ("a", "b"))]


def test_a_run_note_and_a_retired_note_pair_when_their_windows_intersect() -> None:
    """The mixed class: `valid_from`-only (a run) against a closed window (a retired note)."""
    run = _note("run", compound_smiles="CCO", confidence=0.9, valid_from=date(2026, 2, 1))
    retired = _note(
        "old",
        compound_smiles="CCO",
        confidence=0.1,
        valid_from=date(2026, 1, 1),
        valid_to=date(2026, 6, 1),
    )
    # `as_of=None` scans the whole corpus, retired notes included.
    assert [c.pair() for c in find_conflicts([run, retired])] == [("old", "run")]
    # And the same pair with disjoint windows is a history, not a disagreement.
    later = _note("later", compound_smiles="CCO", confidence=0.9, valid_from=date(2026, 7, 1))
    assert find_conflicts([later, retired]) == []


#: How many times the sweep read the two fields it decides overlap on, across one `find_conflicts`.
#:
#: Module-level because an underscore-prefixed class attribute on a pydantic model is a
#: `ModelPrivateAttr`, not a counter.
_FIELD_READS = [0]


class _CountingNote(Note):
    """A note that counts reads of `valid_from` and `valid_to`, which is the sweep's own work.

    Counting rather than timing (as `tests/test_compaction.py::_CountingEstimator`): these are the
    fields `_conditional_disagreements` reads per active note per event, so the count is the work.
    """

    def __getattribute__(self, name: str) -> object:
        if name in ("valid_to", "valid_from"):
            _FIELD_READS[0] += 1
        return object.__getattribute__(self, name)


def _sweep_work(size: int, *, disjoint: bool) -> tuple[int, int]:
    """The field reads one `find_conflicts` costs over `size` dated notes, and what it found.

    The conflicts are returned so the disjoint arm can also assert it finds none.
    """
    base = date(2020, 1, 1)
    notes: list[Note] = []
    for i in range(size):
        start = base + timedelta(days=i)
        notes.append(
            _CountingNote(
                id=f"d{i}",
                type="reaction",
                compound_smiles="CCO",
                confidence=(i % 10) / 10,
                valid_from=start,
                # Disjoint: a one-day window per note, the structure `knowledge/README.md`
                # advertises. Otherwise: every window spans every later one, so every pair is
                # genuinely examined.
                valid_to=start if disjoint else base + timedelta(days=size + i),
            )
        )
    _FIELD_READS[0] = 0
    found = find_conflicts(notes)
    return _FIELD_READS[0], len(found)


def test_a_disjoint_dated_corpus_does_a_linear_amount_of_work() -> None:
    """A disjoint dated corpus does a linear amount of work.

    A one-note-per-day corpus (the structure `knowledge/README.md` advertises) must never examine a
    disjoint pair. Field reads are a deterministic count, about 2x per doubling; eight times the
    corpus is ~8x the work against 64x for the quadratic case. The bar of 10 catches a quadratic arm
    touching more than ~0.05% of the corpus.
    """
    small, small_found = _sweep_work(1_000, disjoint=True)
    large, large_found = _sweep_work(8_000, disjoint=True)
    ratio = large / small

    assert (small_found, large_found) == (0, 0), (
        f"a corpus of closed non-overlapping windows produced {small_found} and {large_found} "
        "conflicts. The sweep never examines a disjoint pair, so every one of these is a pair "
        "whose windows do not overlap being reported as a contradiction"
    )
    assert ratio < 10, (
        f"eight times the corpus cost {large:,} field reads against {small:,} — {ratio:.4f}x, "
        "where linear is 8.0014 and quadratic is 64. The disjoint sweep is examining pairs whose "
        "windows do not overlap again"
    )


def test_the_work_counter_can_see_the_quadratic_arm_it_is_bounding() -> None:
    """The work counter can see the quadratic arm it is bounding.

    The control: on a corpus where every window overlaps every later one the same counter grows ~4x
    per doubling, so the bound above is not satisfied by measuring nothing.
    """
    disjoint, _ = _sweep_work(400, disjoint=True)
    overlapping, overlapping_found = _sweep_work(400, disjoint=False)

    assert overlapping_found > 0, (
        "the overlapping arm found no conflicts, so it is not the populated corpus this control "
        "needs to be one"
    )
    assert overlapping > disjoint * 8, (
        f"a corpus where every window overlaps cost {overlapping:,} field reads against the "
        f"disjoint corpus's {disjoint:,}, so this counter cannot tell the two shapes apart and "
        "the linearity assertion beside it is vacuous"
    )


def test_two_spellings_of_one_molecule_land_in_one_conflict_group() -> None:
    """Grouping canonicalizes the SMILES: `C1CCOC1` and `O1CCCC1` are both THF.

    Grouped on the raw string they never paired, so the detector's recall depended on whoever
    typed the frontmatter — silent under-detection nothing could see.
    """
    left = _note("a", compound_smiles="C1CCOC1", confidence=0.9)
    right = _note("b", compound_smiles="O1CCCC1", confidence=0.1)
    assert [c.pair() for c in find_conflicts([left, right])] == [("a", "b")]


def test_a_self_contradiction_is_not_a_conflict() -> None:
    """`[[contradicts:itself]]` is an authoring mistake, not a disagreement a reader can act on."""
    note = _note("a", body="[[contradicts:a]]")
    assert find_conflicts([note]) == []


def test_failures_against_finds_what_a_design_cites_and_what_it_charges() -> None:
    """`failures_against` finds what a design cites and what it charges.

    - citation is exact: a failure's `contradicts` edge names a note id a design's `EvidenceRef.ref`
      cites.
    - structure is weaker, which is why the check it feeds is a note rather than a blocker.

    The negative arm matters most: failures about other work must come back empty.
    """
    cited = failure_note(
        refutes="playbook-suzuki-a",
        what_happened="the catalyst died above 60 C",
        reported_by="ana",
    )
    structural = failure_note(
        refutes="some-other-note",
        what_happened="the amine oxidised on standing",
        reported_by="ben",
        compound_smiles="CCN",
    )
    unrelated = failure_note(
        refutes="playbook-nothing-to-do-with-us",
        what_happened="a different route entirely",
        reported_by="cat",
    )
    corpus = [cited, structural, unrelated]

    by_citation = failures_against(corpus, cited=["playbook-suzuki-a"])
    assert [note.id for note in by_citation] == [cited.id]

    by_structure = failures_against(corpus, structures=["CCN"])
    assert [note.id for note in by_structure] == [structural.id]

    both = failures_against(corpus, cited=["playbook-suzuki-a"], structures=["CCN"])
    assert {note.id for note in both} == {cited.id, structural.id}

    assert failures_against(corpus, cited=["playbook-unheard-of"]) == []
    assert failures_against(corpus) == [], "asking about nothing must not return everything"


def test_a_failure_matches_a_design_whatever_spelling_the_smiles_arrived_in() -> None:
    """A failure matches a design whatever spelling the SMILES arrived in.

    Notes' `compound_smiles` are not canonicalized on the way in, so both sides are canonicalized
    before comparing, as `kg/conflicts.py` does. Negative arm: ethanol must not match ethylamine.
    """
    spellings = ["OCC", "C(O)C", "[CH3][CH2][OH]", "CCO"]
    corpus = [
        failure_note(
            refutes=f"some-note-{index}",
            what_happened="the alcohol was the wrong nucleophile",
            reported_by="ana",
            compound_smiles=spelling,
        )
        for index, spelling in enumerate(spellings)
    ]
    other = failure_note(
        refutes="some-other-note",
        what_happened="the amine oxidised on standing",
        reported_by="ben",
        compound_smiles="CCN",
    )

    found = failures_against([*corpus, other], structures=["CCO"])

    assert {note.compound_smiles for note in found} == set(spellings), (
        "every spelling of the molecule the design charges has to be the same join key"
    )
    assert failures_against([*corpus, other], structures=["OCC"]) != [], (
        "and the design's own spelling is canonicalized too, not only the note's"
    )
    assert [note.id for note in failures_against([*corpus, other], structures=["CCN"])] == [
        other.id
    ], "ethanol is not ethylamine — canonicalizing must not widen the join to everything"


def test_only_failure_notes_answer_a_failure_query() -> None:
    """Only failure notes answer a failure query.

    Without the type filter every note mentioning the design's evidence would be reported.
    """
    failure = failure_note(refutes="playbook-a", what_happened="it did not hold", reported_by="ana")
    # A `contradicts` edge on a note that is not a failure, so the type filter (not the relation
    # check) is what refuses it: a correction contradicts what it corrects without being a failure
    # record.
    correction = Note(
        id="correction-b",
        type="correction",
        created_by="human",
        source="test",
        body="The published value was wrong: [[contradicts:playbook-a]].\n",
    )

    found = failures_against([failure, correction], cited=["playbook-a"])
    assert [note.id for note in found] == [failure.id], (
        "a correction contradicting the same note is not a record of that note having failed"
    )


def test_a_failure_that_merely_cites_a_note_is_not_a_failure_of_it() -> None:
    """A failure that merely cites a note is not a failure of it.

    A failure note may cite background besides contradicting what failed; matching any edge would
    report the background as having failed.
    """
    failure = Note(
        id="failure-multi",
        type="failure-mode",
        created_by="agent",
        source="feedback:ana",
        tags=["failure-mode"],
        body=(
            "[[contradicts:playbook-a]] did not hold.\n\n"
            "Read against [[cites:review-b]], which is fine.\n"
        ),
    )

    assert [note.id for note in failures_against([failure], cited=["playbook-a"])] == [failure.id]
    assert failures_against([failure], cited=["review-b"]) == [], (
        "the note this failure was read against did not fail; only what it contradicts did"
    )


def test_the_observation_is_what_a_chemist_reads_not_the_provenance_line() -> None:
    """`failure_note` writes the reporter and the date first, then what was seen.

    Surfacing the first line would tell a chemist who filed it and nothing about what happened,
    which is the half with the value in it.
    """
    note = failure_note(
        refutes="playbook-a",
        what_happened="the catalyst died above 60 C",
        reported_by="ana",
    )
    assert observation_of(note) == "the catalyst died above 60 C"
    assert "Reported by" not in observation_of(note)
