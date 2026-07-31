"""The seed knowledge corpus has the shape the rest of the system is built to handle (STO-10).

`knowledge/` used to contain `.gitkeep`, so `make kg-validate` passed by validating nothing and
every crosslink, conflict and relation property was asserted against synthetic fixtures. A test
suite that has never seen a real corpus proves the code runs, not that the corpus works.

These tests are deliberately about *coverage and structure*, not about content: they assert that
every note type and every relation has at least one real instance, that the awkward cases (a
superseded pair, a declared conflict, a calculation crosslink) exist rather than being described,
and that the whole thing validates. They do not assert chemistry — the notes are seed content and
say so.
"""

from datetime import date
from pathlib import Path

from chemclaw.core.config import EvalSettings
from chemclaw.kg.conflicts import find_conflicts
from chemclaw.kg.crosslink import cited_calculations
from chemclaw.kg.graph import build_graph, invalidate_cache, load_notes, related
from chemclaw.kg.note import KNOWN_NOTE_TYPES, Note
from chemclaw.kg.relations import KNOWN_RELATIONS
from chemclaw.kg.validate import validate

_KNOWLEDGE = Path(__file__).resolve().parents[1] / "knowledge"
# Derived from the setting rather than spelled out: D-155 moved the corpus under `data/`, and the
# literal that used to be here would have gone on naming a directory that no longer exists.
_GOLD_CORPUS = (
    Path(__file__).resolve().parents[1]
    / EvalSettings.model_fields["eval_retrieval_corpus_dir"].default
)


def _notes() -> list[Note]:
    """Every note in the shipped corpus, freshly parsed."""
    invalidate_cache()
    return load_notes(_KNOWLEDGE)


def test_the_corpus_validates() -> None:
    """What `make kg-validate` runs in CI, now with something to validate."""
    assert validate(_KNOWLEDGE) == []


def test_the_corpus_is_not_empty() -> None:
    """The regression this exists to prevent: an empty directory that passes every check."""
    notes = _notes()
    assert len(notes) >= 35


def test_every_note_type_has_a_real_instance() -> None:
    """A type nothing in the corpus uses is a type no retrieval filter has ever been exercised on.

    `KNOWN_NOTE_TYPES` was enforced at the gate with no corpus behind it, so a filter keyed on
    `bo-candidate` or `failure-mode` could have been broken indefinitely without a failing test.
    """
    present = {note.type for note in _notes()}
    missing = sorted(KNOWN_NOTE_TYPES - present)
    assert not missing, f"never instantiated: {missing}"


def test_every_known_relation_has_a_real_instance() -> None:
    """Same argument for edges: a vocabulary entry with no instance is untested surface."""
    used = {relation.rel for note in _notes() for relation in note.outgoing_relations()}
    assert not KNOWN_RELATIONS - used, f"never used: {sorted(KNOWN_RELATIONS - used)}"


def test_the_graph_is_connected_enough_to_traverse() -> None:
    """A corpus of islands would validate perfectly and exercise no graph query at all."""
    graph = build_graph(_KNOWLEDGE)
    assert graph.number_of_edges() >= 40
    # The query typed edges were added for, against real content rather than a fixture.
    assert related(graph, "rxn-suzuki-biaryl", "precursor-of") == [
        "compound-4-bromoanisole",
        "compound-phenylboronic-acid",
    ]


def test_a_superseded_note_is_excluded_from_current_evidence() -> None:
    """Bi-temporality with something real to exclude — at both the note and the edge level."""
    notes = {note.id: note for note in _notes()}
    retired = notes["playbook-degassing-old"]
    assert retired.valid_to is not None
    assert not retired.is_current(date.today())
    # And it points forward, so a reader holding the old note can find the new one.
    assert related(build_graph(_KNOWLEDGE), retired.id, "superseded-by") == ["playbook-degassing"]


def test_the_corpus_contains_a_declared_conflict() -> None:
    """`kg.conflicts` needs a real disagreement to find, or it is only tested on fixtures."""
    conflicts = find_conflicts(_notes(), as_of=date.today())
    assert conflicts, "the seed corpus asserts no conflict, so nothing exercises the detector"
    assert any(conflict.kind == "declared" for conflict in conflicts)


def test_a_computed_note_cites_the_calculation_behind_it() -> None:
    """The crosslink (STO-7) with real content on both ends of it."""
    notes = {note.id: note for note in _notes()}
    computed = notes["job-aspirin-thermo"]
    assert computed.calc_refs
    assert computed.artifact_refs
    # An artifact citation implies the run that produced it, so both keys resolve from one note.
    assert len(cited_calculations(computed)) == 2


def test_the_seed_corpus_and_the_eval_corpus_stay_separate() -> None:
    """`data/evals/retrieval_corpus/` must not be absorbed into the live graph.

    Its own README says why: keeping the gold corpus outside `knowledge_dir` is what makes the
    recall/precision numbers reproducible and independent of whatever is in the live graph. The
    original plan proposed *promoting* those fixtures here, which would have coupled every pinned
    eval number to an edit of the seed content.
    """
    seeded = {note.id for note in _notes()}
    invalidate_cache()
    gold = {note.id for note in load_notes(_GOLD_CORPUS)}
    assert gold, f"the gold corpus is not at {_GOLD_CORPUS}; an empty set intersects nothing"
    assert not seeded & gold
