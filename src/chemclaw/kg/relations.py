"""What an edge in the knowledge graph is allowed to mean.

Typed edges let queries ask for a compound's precursors, a contradicting note or the calculation
behind a claim. The vocabulary is adopted from RXNO, CHMO, CHEMINF and OntoRXN rather than invented.
Enforced by `kg.validate` over the corpus rather than by the `Note` schema, so a deployment
extending the vocabulary does not fail the agent's write at the tool.
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
        # This experiment was run in response to that one. Minted by whoever can read
        # the intent, never derived from two dates: a date proves sequence, not response.
        "follows",
        "contradicts",  # this note asserts something incompatible with that one
        "supersedes",  # this note replaces that one as the current answer
        "superseded-by",  # the inverse, so the older note can point forward
        # --- grouping ---
        "part-of",  # this note belongs to that campaign, report or collection
    }
)


# The direction each typed relation runs in, as the note types allowed at each end; `None` leaves an
# end unconstrained. Only relations with a stated direction are listed (`analogue-of`,
# `contradicts`, `cites` connect anything). Enforced by `kg.validate` where both endpoints resolve
# in the corpus.
RELATION_SIGNATURES: dict[str, tuple[frozenset[str] | None, frozenset[str] | None]] = {
    "precursor-of": (frozenset({"compound"}), frozenset({"compound"})),
    "product-of": (frozenset({"compound"}), frozenset({"reaction"})),
    "reagent-in": (frozenset({"compound"}), frozenset({"reaction"})),
    "catalyzes": (frozenset({"compound"}), frozenset({"reaction"})),
    "solvent-for": (frozenset({"compound"}), frozenset({"reaction"})),
    "part-of": (None, frozenset({"campaign", "optimization-campaign", "report"})),
    # The target of `measured-by` is an experimental method or instrument, so only an
    # `analytical-method` note is a legal target.
    "measured-by": (None, frozenset({"analytical-method"})),
}


def known_relations() -> frozenset[str]:
    """The adopted vocabulary plus the relations the enabled connector bundles declare.

    The relation-side twin of `chemclaw.kg.note.known_note_types`; see that function.
    """
    from chemclaw.connectors.registry import declared_relations

    return KNOWN_RELATIONS | declared_relations()
