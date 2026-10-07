"""Structural questions about the graph as a whole: what don't we know.

Traversal from a hit answers "what do we know about X"; these reads over the parsed note set answer
where knowledge is missing, to seed experimental design. No new store or index. Three shapes of gap:

- Isolated notes: linked to nothing, so unreachable by graph traversal.
- Thin areas: a note type or tag with evidence but no distillation above it, where synthesis is
  owed.
- Hubs: the most-cited notes, where an error propagates furthest and a reviewer should look first.
"""

from collections import Counter

import networkx as nx
from pydantic import BaseModel, Field

from chemclaw.core.config import settings
from chemclaw.kg.graph import dangling_links
from chemclaw.kg.note import UNDISTILLED_TAG, Note


class GraphGaps(BaseModel):
    """Where the corpus is thin, unreachable, or load-bearing."""

    total_notes: int
    isolated_note_ids: list[str] = Field(default_factory=list)
    type_counts: dict[str, int] = Field(default_factory=dict)
    # Free-text `note.tags`, not projects: `Note` has no project field, and the name is all the
    # model has to go on.
    tags_without_distillation: list[str] = Field(default_factory=list)
    # Playbooks that record a recurrence but state no rule yet: unlike the field above (topics with
    # no playbook), these have a note id to act on.
    undistilled_playbook_ids: list[str] = Field(default_factory=list)
    most_cited: list[tuple[str, int]] = Field(default_factory=list)
    dangling_links: list[str] = Field(default_factory=list)


# Note types that *distil* rather than *record*. A tag carrying only recording-type notes has
# evidence nobody has generalized yet — the concrete thing "what don't we know" should surface.
_DISTILLED_TYPES = frozenset({"playbook", "optimization-campaign", "campaign", "report"})


def analyze(graph: nx.DiGraph, notes: list[Note], *, top_n: int | None = None) -> GraphGaps:
    """Summarize the graph's structural gaps.

    Args:
        graph: The indexed note graph (`chemclaw.kg.graph.build_graph`).
        notes: The parsed notes behind it, for the metadata the graph nodes do not carry.
        top_n: How many hubs to report; `graph_analytics_top_n` by default.

    Returns:
        The gap summary. Every list is sorted, so the result is deterministic and diffable.
    """
    hubs = settings.graph_analytics_top_n if top_n is None else top_n
    by_type = Counter(note.type for note in notes)
    return GraphGaps(
        total_notes=len(notes),
        # A self-link is not a connection: such a note is as invisible to traversal as one with no
        # edges.
        isolated_note_ids=sorted(
            node
            for node in graph.nodes
            if all(neighbour == node for neighbour in graph.predecessors(node))
            and all(neighbour == node for neighbour in graph.successors(node))
        ),
        type_counts=dict(sorted(by_type.items())),
        tags_without_distillation=_undistilled_tags(notes),
        undistilled_playbook_ids=sorted(note.id for note in notes if UNDISTILLED_TAG in note.tags),
        most_cited=_hubs(graph, hubs),
        dangling_links=[f"{source} -> {target}" for source, target in dangling_links(notes)],
    )


def _undistilled_tags(notes: list[Note]) -> list[str]:
    """Tags that carry recorded evidence but nothing distilled from it.

    A backlog for the synthesis layer, not a defect. A set difference over free-text tags, so values
    are topics like `suzuki`, not projects.
    """
    evidence: set[str] = set()
    distilled: set[str] = set()
    for note in notes:
        target = distilled if note.type in _DISTILLED_TYPES else evidence
        target.update(note.tags)
    return sorted(evidence - distilled)


def _hubs(graph: nx.DiGraph, top_n: int) -> list[tuple[str, int]]:
    """The most-cited notes, most first. Ties break by id so the result is deterministic.

    Only nodes that carry a note: a dangling link target would otherwise rank as a hub by the very
    citations that make it dangling. Those are reported separately in `GraphGaps.dangling_links`.
    """
    ranked = sorted(
        (
            (node, graph.in_degree(node))
            for node in graph.nodes
            if graph.nodes[node].get("note") is not None
        ),
        key=lambda item: (-item[1], item[0]),
    )
    return [(node, degree) for node, degree in ranked[:top_n] if degree > 0]
