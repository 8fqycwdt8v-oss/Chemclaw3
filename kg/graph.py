"""NetworkX index of the knowledge graph (plan step 2.3).

Builds a directed graph from a directory of notes: nodes are note ids (each
carrying its parsed `Note`), edges are `[[wikilink]]` relations. Retrieval is
graph traversal (D-004), so this indexer is the substrate the query skill walks
(1–2 hops), not a vector index.
"""

from pathlib import Path

import networkx as nx

from kg.note import Note, read_note


def load_notes(notes_dir: Path) -> list[Note]:
    """Parse every note under `notes_dir` (recursively), skipping non-note files."""
    notes = []
    for path in sorted(notes_dir.rglob("*.md")):
        note = read_note(path)
        if note is not None:
            notes.append(note)
    return notes


def build_graph(notes_dir: Path) -> nx.DiGraph:
    """Build the directed note graph from `notes_dir`.

    Every note becomes a node keyed by its id with the `Note` on the `note`
    attribute. Each `[[wikilink]]` becomes an edge id → target. A link to an
    unknown id still creates the edge (a dangling node with no `note` attribute),
    so `kg.validate` can report it rather than the graph silently dropping it.
    """
    graph: nx.DiGraph = nx.DiGraph()
    notes = load_notes(notes_dir)
    for note in notes:
        graph.add_node(note.id, note=note)
    for note in notes:
        for target in note.outgoing_links():
            graph.add_edge(note.id, target)
    return graph


def neighborhood(graph: nx.DiGraph, note_id: str, hops: int = 1) -> set[str]:
    """Return note ids within `hops` of `note_id`, following links both ways.

    Chemical relations are meaningful in both directions (a precursor and a
    product reference each other), so traversal is undirected over the directed
    graph — the 1–2 hop expansion the query skill uses (D-004).
    """
    if note_id not in graph:
        raise KeyError(f"unknown note id: {note_id!r}")
    undirected = graph.to_undirected(as_view=True)
    lengths = nx.single_source_shortest_path_length(undirected, note_id, cutoff=hops)
    return set(lengths) - {note_id}
