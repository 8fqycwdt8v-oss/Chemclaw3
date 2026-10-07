"""The seed knowledge corpus has the shape the rest of the system is built to handle.

These tests assert coverage and structure, not chemistry: every note type and relation has a
real instance, the awkward cases (a superseded pair, a declared conflict, a calculation
crosslink) exist, and the whole corpus validates.
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
    """Every note type has a real instance, so every retrieval filter is exercised on real content.

    Checked against the effective vocabulary — core's set unioned with what enabled bundles declare.
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
    # The typed-edge queries against real content, in the direction the vocabulary declares.
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
    """The corpus carries edge metadata that actually reaches a reader.

    Asserted over `outgoing_relations`, what the graph is built from, so confidence and validity
    windows declared in frontmatter must survive parsing.
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


def test_the_seed_corpus_cites_no_calculation_the_store_cannot_back() -> None:
    """The seed corpus cites no calculation the store cannot back.

    `kg-validate` checks every `calc_ref` exists, so seed job-results must say in prose why their
    refs are empty; `tests/test_crosslink.py` proves the crosslink with real keys.
    """
    for note in _notes():
        assert cited_calculations(note) == [], (
            f"seed note {note.id!r} cites a calculation no store holds; "
            "kg-validate fails on it against any fresh database"
        )


def test_the_seed_corpus_and_the_eval_corpus_stay_separate() -> None:
    """`data/evals/retrieval_corpus/` stays outside the live graph, so eval numbers stay
    reproducible.
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
    """A seed reaction records its figures in `conditions`, where comparative tools can read them.

    At least one figure, not all three: a note may record only what its prose states, and a ramp
    or "reflux" is not a setpoint.
    """
    for note in _seed_reactions():
        assert note.conditions is not None, (
            f"{note.id} states its run in prose alone; transcribe the figures it gives into "
            "`conditions`, or say in the note why it describes no performed run"
        )
        recorded = note.conditions.model_dump(exclude_none=True)
        assert recorded, f"{note.id} carries an empty conditions block, which claims nothing"


def test_the_corpus_exercises_the_comparison_that_needs_no_model() -> None:
    """The deterministic half of `condense_protocols` runs over the real corpus with no client.

    `drop_empty_columns` would silently remove columns whose figures live only in prose.
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
