"""Structural questions about the graph as a whole (gap KNW-5).

`kg/graph.py` exposes exactly `build_graph` and `neighborhood`: you can walk *outward from a hit*.
That answers "what do we know about X" and can never answer **"what don't we know"** — which is the
question that actually steers experimental design, and the one `suggest_next_experiment` should be
seeded from instead of a decision space the model assembles by hand from prose evidence.

Everything here is a read over the already-parsed note set, so it adds no store and no index: the
data to answer these questions was always present, only unaskable.

Three deliberately different shapes of "gap", because they fail differently:

- **Isolated notes** — knowledge nobody linked to anything. Retrieval reaches these only by a
  literal substring hit, so they are effectively invisible to graph traversal (D-004's reasoning
  path); they are not *missing*, they are unreachable.
- **Thin areas** — a note type or *tag* with evidence but no distillation above it (runs with no
  playbook, a topic with no report). These are where synthesis is owed.
- **Hubs** — the notes everything else cites. Useful for the opposite reason: they are where an
  error propagates furthest, so they are what a reviewer should check first.
"""

from collections import Counter

import networkx as nx
from pydantic import BaseModel, Field

from chemclaw.core.config import settings
from chemclaw.kg.graph import dangling_links
from chemclaw.kg.note import Note


class GraphGaps(BaseModel):
    """Where the corpus is thin, unreachable, or load-bearing."""

    total_notes: int
    isolated_note_ids: list[str] = Field(default_factory=list)
    type_counts: dict[str, int] = Field(default_factory=dict)
    # Free-text `note.tags`, not projects: there is no project field on `Note` at all. The field
    # was called `projects_without_distillation`, and the name — not the computation, which is
    # right — is what let a live run report "27 projects tagged" as a portfolio status. A field
    # name is an assertion about what the values *are*, and the model has nothing else to go on.
    tags_without_distillation: list[str] = Field(default_factory=list)
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
        top_n: How many hubs to report; the configured `graph_analytics_top_n` by default. It was
            a literal `5` with no caller ever passing anything else, which made the one number in
            this module that shapes a model-facing result the only one an operator could not
            change — unlike its siblings `graph_max_results` and `graph_max_hops`.

    Returns:
        The gap summary. Every list is sorted, so the result is deterministic and diffable.
    """
    hubs = settings.graph_analytics_top_n if top_n is None else top_n
    by_type = Counter(note.type for note in notes)
    return GraphGaps(
        total_notes=len(notes),
        isolated_note_ids=sorted(
            node
            for node in graph.nodes
            if graph.in_degree(node) == 0 and graph.out_degree(node) == 0
        ),
        type_counts=dict(sorted(by_type.items())),
        tags_without_distillation=_undistilled_tags(notes),
        most_cited=_hubs(graph, hubs),
        dangling_links=[f"{source} -> {target}" for source, target in dangling_links(notes)],
    )


def _undistilled_tags(notes: list[Note]) -> list[str]:
    """Tags that carry recorded evidence but nothing distilled from it.

    A topic with runs and no playbook/campaign/report is not a defect — it is a *backlog item for
    the synthesis layer*, and naming it is the difference between the memory jobs being trusted and
    merely running.

    Tags, and only tags: this is a set difference over free-text `note.tags`, so on the committed
    corpus it returns `suzuki`, `palladium`, `pka` and the like. Calling them projects was the
    whole defect — the computation was always correct about what it measured.
    """
    evidence: set[str] = set()
    distilled: set[str] = set()
    for note in notes:
        target = distilled if note.type in _DISTILLED_TYPES else evidence
        target.update(note.tags)
    return sorted(evidence - distilled)


def _hubs(graph: nx.DiGraph, top_n: int) -> list[tuple[str, int]]:
    """The most-cited *notes*, most first. Ties break by id so the result is deterministic.

    **Only nodes that carry a note.** `build_graph` deliberately keeps a link to an unknown id as a
    node with no `note` attribute, so `kg-validate` can report it — and this ranked those nodes
    too, by the very citations that make them dangling. Measured on a corpus where four notes cite
    a `compound-pending` that does not exist: it came back as *the most-cited note in the graph*.
    That is not an exotic corruption but the state D-018 describes as normal — a fingerprint-indexed
    reaction is citable before its note clears the PR-gate — so `find_knowledge_gaps` was telling a
    chemist to check the hub that matters most, and `expand_note` on it raised.

    A dangling target is not silently dropped by this filter, it is reported as what it is: it has
    its own field, `GraphGaps.dangling_links`, which the same `analyze` fills from the same graph.
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
