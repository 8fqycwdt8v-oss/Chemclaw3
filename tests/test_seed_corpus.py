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
from chemclaw.kg.note import Note, known_note_types
from chemclaw.kg.relations import known_relations
from chemclaw.kg.validate import validate

_KNOWLEDGE = Path(__file__).resolve().parents[1] / "knowledge"
# Derived from the setting rather than spelled out: D-156 moved the corpus under `data/`, and the
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

    Checked against the *effective* vocabulary — core's set unioned with what the enabled bundles
    declare — so that moving `job-result` and `bo-candidate` out of core's frozenset and into the
    `qm` and `bo` manifests does not quietly drop them from this guarantee. They are exactly the
    two types the docstring above names, so checking core's half alone would have retired the test's
    own example.
    """
    present = {note.type for note in _notes()}
    missing = sorted(known_note_types() - present)
    assert not missing, f"never instantiated: {missing}"


def test_every_known_relation_has_a_real_instance() -> None:
    """Same argument for edges: a vocabulary entry with no instance is untested surface."""
    used = {relation.rel for note in _notes() for relation in note.outgoing_relations()}
    assert not known_relations() - used, f"never used: {sorted(known_relations() - used)}"


def test_the_graph_is_connected_enough_to_traverse() -> None:
    """A corpus of islands would validate perfectly and exercise no graph query at all."""
    graph = build_graph(_KNOWLEDGE)
    assert graph.number_of_edges() >= 40
    # The queries typed edges were added for, against real content rather than a fixture — in the
    # direction the vocabulary declares. This test used to pin the *inverse* (`precursor-of`
    # asserted from the reaction toward its starting materials), which is how twelve backwards
    # edges merged green and `product-of` came to point both ways in one graph
    # (`docs/archive/REVIEW-2026-08-27-knowledge-system-analysis.md` §1).
    assert related(graph, "compound-4-bromoanisole", "precursor-of") == [
        "compound-4-methoxybiphenyl",
    ]
    assert related(graph, "rxn-suzuki-biaryl", "part-of") == ["campaign-biaryl-scope"]
    assert related(graph, "compound-pd-oac2", "catalyzes") == [
        "rxn-buchwald-amination",
        "rxn-suzuki-biaryl",
    ]


def test_a_superseded_note_is_excluded_from_current_evidence() -> None:
    """Bi-temporality with something real to exclude — at both the note and the edge level."""
    notes = {note.id: note for note in _notes()}
    retired = notes["playbook-degassing-old"]
    assert retired.valid_to is not None
    assert not retired.is_current(date.today())
    # And it points forward, so a reader holding the old note can find the new one.
    assert related(build_graph(_KNOWLEDGE), retired.id, "superseded-by") == ["playbook-degassing"]


def test_the_corpus_carries_edge_metadata_that_actually_reaches_a_reader() -> None:
    """The exemplars for STO-9 must be live, not merely written down.

    Every note here that declares a frontmatter relation also writes the same link in its body,
    which used to mean the body form won the deduplication and the confidence and validity window
    were dropped at parse time. The corpus therefore *documented* edge metadata and contained none
    a query could see: `related(..., as_of=)` had no dated edge to filter, and every
    `Relation.confidence` in the graph read as `None`.

    Asserted over `outgoing_relations` — what the graph is built from — rather than over
    `note.relations`, because the frontmatter was never the thing that was lost.
    """
    edges = [relation for note in _notes() for relation in note.outgoing_relations()]
    assert any(relation.confidence is not None for relation in edges), (
        "no edge in the corpus carries a confidence a reader can see"
    )
    assert any(
        relation.valid_from is not None or relation.valid_to is not None for relation in edges
    ), "no edge in the corpus carries a validity window, so STO-9 is exercised by nothing"


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


def _seed_reactions() -> list[Note]:
    """Every `reaction` note in the shipped corpus."""
    notes = [n for n in load_notes(_KNOWLEDGE) if n.type == "reaction"]
    assert notes, "the seed corpus must hold reaction notes"
    return notes


def test_a_seed_reaction_records_its_figures_where_a_machine_can_read_them() -> None:
    """A figure stated only in prose is a figure the comparative tools cannot use.

    Every one of these notes describes a run that was performed and states its temperature, time or
    yield in the body — and for a while every one of them stated it *only* there. `conditions` is
    the frontmatter form those tools read (`ProcessConditions` says why), so a seed note that keeps
    its numbers in sentences is a corpus that cannot exercise the half of the comparison built to
    need no model at all.

    At least one figure, not all three: what a note may claim is bounded by what its prose says.
    Two of these record no temperature because their prose gives none — "0 °C to rt" is a ramp and
    "reflux" is not a setpoint — and supplying a number the record does not contain is the one thing
    every artifact downstream refuses to do.
    """
    for note in _seed_reactions():
        assert note.conditions is not None, (
            f"{note.id} states its run in prose alone; transcribe the figures it gives into "
            "`conditions`, or say in the note why it describes no performed run"
        )
        recorded = note.conditions.model_dump(exclude_none=True)
        assert recorded, f"{note.id} carries an empty conditions block, which claims nothing"


def test_the_corpus_exercises_the_comparison_that_needs_no_model() -> None:
    """The deterministic half of `condense_protocols`, over the real corpus, with no client.

    This is the property the frontmatter exists for, asserted end to end rather than on a field:
    `drop_empty_columns` removes a column no protocol recorded, so a corpus whose figures live in
    prose renders a comparison with the recorded columns silently gone — which is exactly what
    every local run and demo showed before the transcription. Driven with `client=None`, because
    the point is that this half answers from the record and needs no credential.
    """
    import asyncio

    from chemclaw.agent.condense import Protocol, condense_protocols

    protocols = [
        Protocol(ref=note.id, conditions=note.conditions, text="") for note in _seed_reactions()
    ]
    table = asyncio.run(condense_protocols(protocols, client=None)).table

    for column in ("Temp (°C)", "Time (h)", "Yield (%)"):
        assert column in table, (
            f"no seed reaction records {column}, so the comparison drops it — the deterministic "
            "half of the digest is unexercised by the corpus it ships with"
        )
    assert "→" in table, "with recorded figures the changes column must report a real change"
