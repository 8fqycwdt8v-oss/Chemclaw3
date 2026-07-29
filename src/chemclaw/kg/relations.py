"""What an edge in the knowledge graph is allowed to mean (STO-8).

Every edge used to be `graph.add_edge(note.id, target)` — no attributes at all. A graph in which
nothing can be said *about* a connection is a citation network, and the retrieval layer treated it
as one: no query could ask for a compound's precursors, for the note that contradicts this one, or
for the calculation a claim was computed from. The links were there; the relations were not.

The vocabulary below is **adopted, not invented**. RXNO (reaction ontology), CHMO (chemical
methods), CHEMINF (chemical information) and OntoRXN already name these relations for chemical
knowledge graphs, and a bespoke set would be one more thing to map to a standard later.

Enforced by `chemclaw.kg.validate`, not by the `Note` schema — exactly as `KNOWN_NOTE_TYPES` is,
and for the
same reason. The agent must be able to *propose* a relation this list does not have; the PR-gate is
where a human decides whether it joins the vocabulary, and `kg-validate` runs on that same PR, so
an unintended relation cannot reach the graph unreviewed while an intended one costs one line here.
"""

# The default relation. A bare `[[wikilink]]` is a citation and nothing stronger — which is what
# every existing note in the corpus means by one, so the migration is that they keep meaning it.
DEFAULT_RELATION = "cites"

KNOWN_RELATIONS: frozenset[str] = frozenset(
    {
        DEFAULT_RELATION,  # this note refers to that one; the untyped link's meaning
        # --- structure and synthesis (RXNO / OntoRXN) ---
        "precursor-of",  # this compound is a starting material for that one
        "product-of",  # this compound is produced by that reaction
        "reagent-in",  # this compound is consumed by that reaction without being the substrate
        "catalyzes",  # this species accelerates that reaction without being consumed
        "solvent-for",  # this compound is the medium that reaction runs in
        "analogue-of",  # structurally related enough that evidence may transfer
        # --- evidence and method (CHMO / CHEMINF) ---
        "measured-by",  # this claim rests on that experimental method or instrument
        "computed-from",  # this claim was derived from that calculation or note
        "evidence-for",  # this note supports that claim
        # --- disagreement and time ---
        "contradicts",  # this note asserts something incompatible with that one
        "supersedes",  # this note replaces that one as the current answer
        "superseded-by",  # the inverse, so the older note can point forward
        # --- grouping ---
        "part-of",  # this note belongs to that campaign, report or collection
    }
)
