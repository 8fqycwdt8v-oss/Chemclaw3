"""Typed edges and edge-level validity (STO-8, STO-9).

Every edge in the graph was `add_edge(note.id, target)` with no attributes at all, so no relation
could say *precursor-of*, *contradicts* or *computed-from*. A knowledge graph in which nothing can
be said about a connection is a citation network, and the retrieval layer treated it as one.

Two properties matter most here and both are asserted below: that the existing corpus is unchanged
by the addition (a bare `[[link]]` still means exactly what it meant), and that an unknown relation
fails at the gate rather than becoming an edge nothing can find.
"""

from datetime import date
from pathlib import Path

import pytest

from kg.graph import build_graph, invalidate_cache, related
from kg.note import Note, Relation, cited_ids, cited_links, split_link
from kg.relations import DEFAULT_RELATION, KNOWN_RELATIONS
from kg.render import render_note
from kg.validate import validate


def _write(directory: Path, *notes: Note) -> Path:
    """Write notes into a fresh knowledge tree and return its root."""
    root = directory / "knowledge"
    for note in notes:
        path = root / note.type / f"{note.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_note(note), encoding="utf-8")
    invalidate_cache()
    return root


def test_a_bare_wikilink_still_means_exactly_what_it_meant() -> None:
    """Backward compatibility, asserted rather than assumed.

    Every note in the corpus predates typed edges. If a bare link stopped resolving to its target,
    or started carrying a relation nobody wrote, this change would silently rewrite history.
    """
    assert split_link("compound-x") == (DEFAULT_RELATION, "compound-x")
    assert cited_ids("see [[compound-x]] and [[compound-y]]") == ["compound-x", "compound-y"]
    assert cited_links("see [[compound-x]]") == [(DEFAULT_RELATION, "compound-x")]


def test_the_shipped_corpus_parses_to_the_same_edges_it_always_did() -> None:
    """The real fixture corpus, not a synthetic one — the compatibility claim's actual subject."""
    corpus = Path(__file__).resolve().parents[1] / "evals" / "retrieval_corpus"
    invalidate_cache()
    graph = build_graph(corpus)
    assert graph.number_of_nodes() > 0
    for _, _, data in graph.edges(data=True):
        # Every pre-existing link is a citation and nothing stronger.
        assert [relation.rel for relation in data["relations"]] == [DEFAULT_RELATION]


def test_a_typed_link_becomes_a_typed_edge(tmp_path: Path) -> None:
    """The syntax `_SLUG` left free: `[[rel:target]]` used to be one dangling id."""
    source = Note(id="rxn-a", type="reaction", body="from [[precursor-of:compound-b]]")
    target = Note(id="compound-b", type="compound")
    graph = build_graph(_write(tmp_path, source, target))

    assert graph.has_edge("rxn-a", "compound-b")
    relations = graph["rxn-a"]["compound-b"]["relations"]
    assert [relation.rel for relation in relations] == ["precursor-of"]
    # The untyped view is unchanged, which is what keeps `kg.validate` and the answer verifier
    # working through one code path rather than two.
    assert source.outgoing_links() == ["compound-b"]


def test_frontmatter_relations_are_edges_too_and_carry_what_a_link_cannot(tmp_path: Path) -> None:
    """The structured form exists for the metadata `[[rel:target]]` has no room for."""
    source = Note(
        id="note-a",
        type="report",
        relations=[
            Relation(rel="contradicts", to="note-b", confidence=0.8, valid_from=date(2026, 1, 1))
        ],
    )
    graph = build_graph(_write(tmp_path, source, Note(id="note-b", type="report")))

    relation = graph["note-a"]["note-b"]["relations"][0]
    assert (relation.rel, relation.confidence) == ("contradicts", 0.8)
    assert relation.valid_from == date(2026, 1, 1)


def test_a_frontmatter_relation_to_a_missing_note_dangles_like_a_body_link(tmp_path: Path) -> None:
    """Both forms are held to the same standard, or the structured one becomes a way around it."""
    source = Note(id="note-a", type="report", relations=[Relation(rel="cites", to="note-gone")])
    problems = validate(_write(tmp_path, source))
    assert any("unknown note 'note-gone'" in problem for problem in problems)


def test_an_unknown_relation_fails_validation(tmp_path: Path) -> None:
    """A typo makes an edge no relation-aware query can find, so it stops at the gate.

    Checked in `kg.validate` rather than in the schema, exactly as `KNOWN_NOTE_TYPES` is: the agent
    must still be able to *propose* a genuinely new relation, and a human sees it on the PR.
    """
    source = Note(id="a", type="report", body="[[percursor-of:b]]")  # typo, deliberately
    problems = validate(_write(tmp_path, source, Note(id="b", type="compound")))
    assert any("unknown relation 'percursor-of'" in problem for problem in problems)


def test_every_known_relation_is_accepted(tmp_path: Path) -> None:
    """The vocabulary and the validator agree — a relation listed but rejected would be a trap."""
    target = Note(id="b", type="compound")
    for relation in sorted(KNOWN_RELATIONS):
        source = Note(id="a", type="report", relations=[Relation(rel=relation, to="b")])
        assert validate(_write(tmp_path, source, target)) == [], relation


def test_two_relations_between_the_same_pair_both_survive(tmp_path: Path) -> None:
    """The cost of staying on `DiGraph`: an edge holds a tuple of relations, not one.

    A compound can be both a precursor and a product of the same reaction. Collapsing that to one
    edge attribute would lose the information typing exists to record — so the attribute is plural.
    """
    source = Note(id="rxn", type="reaction", body="[[precursor-of:c]] and also [[product-of:c]]")
    graph = build_graph(_write(tmp_path, source, Note(id="c", type="compound")))
    relations = graph["rxn"]["c"]["relations"]
    assert sorted(relation.rel for relation in relations) == ["precursor-of", "product-of"]


def test_the_same_edge_written_both_ways_is_not_doubled(tmp_path: Path) -> None:
    """Writing an edge in the body and in frontmatter is harmless, not two edges."""
    source = Note(
        id="a",
        type="report",
        body="[[contradicts:b]]",
        relations=[Relation(rel="contradicts", to="b")],
    )
    graph = build_graph(_write(tmp_path, source, Note(id="b", type="report")))
    assert len(graph["a"]["b"]["relations"]) == 1


def test_related_answers_the_query_typed_edges_exist_for(tmp_path: Path) -> None:
    """What are this reaction's precursors? Impossible to ask before typed edges."""
    source = Note(id="rxn", type="reaction", body="[[precursor-of:c1]] [[product-of:c2]]")
    graph = build_graph(
        _write(tmp_path, source, Note(id="c1", type="compound"), Note(id="c2", type="compound"))
    )
    assert related(graph, "rxn", "precursor-of") == ["c1"]
    assert related(graph, "rxn", "product-of") == ["c2"]
    assert related(graph, "rxn", "catalyzes") == []


def test_related_is_directed_because_a_relation_is(tmp_path: Path) -> None:
    """Unlike `neighborhood`, which traverses both ways on purpose.

    `precursor-of` reversed is not `precursor-of`. A query that silently walked backwards would
    return products as precursors.
    """
    source = Note(id="rxn", type="reaction", body="[[precursor-of:c1]]")
    graph = build_graph(_write(tmp_path, source, Note(id="c1", type="compound")))
    assert related(graph, "c1", "precursor-of") == []


def test_an_edge_can_stop_being_true_while_both_notes_stay_current(tmp_path: Path) -> None:
    """Edge-level bi-temporality (STO-9) — the half that was missing.

    `Note.valid_from`/`valid_to` could say a *fact* expired. Nothing could say a *relation* did:
    that this catalyst was used for that transformation until the process changed, while both notes
    remain perfectly current. The edge stays in git and in the graph; it is only excluded from a
    current-evidence query.
    """
    source = Note(
        id="rxn",
        type="reaction",
        relations=[Relation(rel="catalyzes", to="cat", valid_to=date(2025, 6, 30))],
    )
    graph = build_graph(_write(tmp_path, source, Note(id="cat", type="compound")))

    assert related(graph, "rxn", "catalyzes", as_of=date(2025, 1, 1)) == ["cat"]
    assert related(graph, "rxn", "catalyzes", as_of=date(2026, 1, 1)) == []
    # Without an `as_of` the assertion is still visible — history is not deleted.
    assert related(graph, "rxn", "catalyzes") == ["cat"]


def test_an_edge_window_that_ends_before_it_starts_is_rejected() -> None:
    """The same nonsense-window check a note gets, because the same query would break on it."""
    with pytest.raises(ValueError, match="valid_to .* is before valid_from"):
        Relation(rel="cites", to="b", valid_from=date(2026, 2, 1), valid_to=date(2026, 1, 1))


def test_related_on_an_unknown_note_says_so(tmp_path: Path) -> None:
    """A missing id is a caller error, not an empty result that reads like "no precursors"."""
    graph = build_graph(_write(tmp_path, Note(id="a", type="report")))
    with pytest.raises(KeyError, match="unknown note id"):
        related(graph, "nope", "cites")


def test_a_malformed_typed_link_is_reported_as_written(tmp_path: Path) -> None:
    """`[[rel:]]` and `[[:x]]` are not silently repaired.

    Repairing them would hide a typo behind a link that resolves to something the author did not
    write. Failing with the literal text is what makes the mistake findable.
    """
    assert split_link("precursor-of:") == (DEFAULT_RELATION, "precursor-of:")
    assert split_link(":compound-x") == (DEFAULT_RELATION, ":compound-x")
    problems = validate(_write(tmp_path, Note(id="a", type="report", body="[[precursor-of:]]")))
    assert any("precursor-of:" in problem for problem in problems)
