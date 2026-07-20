"""Build a `campaign` note from a detected chain (plan steps 5.1, 5.3, deterministic core).

The episodic memory note: it narrates a chain of experiments and **cites every one** via a
`[[reaction-<id>]]` wikilink. **Precondition:** synthesis runs over reactions whose reaction
notes are already merged into the graph (the operational order is ELN-sync → human merges the
reaction notes → memory synthesis), so the citations resolve; a campaign PR for a reaction not
yet merged would dangle and `kg-validate` would reject it — the gate enforces the ordering, it
is not assumed silently. This builder produces the citable, factual skeleton (the
transformation sequence and its evidence); the richer prose narrative is the
`campaign-narrative-synthesis` skill's judgment (per plan 5.3), layered on top, not invented here.
"""

import hashlib

from eln.ord import OrdReaction
from kg.note import Note
from memory.chains import Chain


def _campaign_id(reaction_ids: list[str]) -> str:
    """Stable campaign id from its member reactions, so re-synthesis is idempotent."""
    digest = hashlib.sha256("|".join(sorted(reaction_ids)).encode()).hexdigest()[:12]
    return f"campaign-{digest}"


def campaign_note_from_chain(chain: Chain, reactions: dict[str, OrdReaction]) -> Note:
    """Map a chain to an agent `campaign` note that links to each member reaction.

    `reactions` maps reaction id → the `OrdReaction`, for the per-step SMILES. The body
    lists the chain in order, each step wikilinking its reaction note (the evidence), then
    the product→reactant handoffs that make it a campaign. The project (if the members share
    one) is carried so the semantic layer can group campaigns across projects.
    """
    steps = []
    for position, reaction_id in enumerate(chain.reaction_ids, start=1):
        reaction = reactions[reaction_id]
        steps.append(f"{position}. [[reaction-{reaction_id}]]: `{reaction.reaction_smiles()}`")
    handoffs = [
        f"- {link.via_compound} (product of {link.from_reaction} → reactant of {link.to_reaction})"
        for link in chain.links
    ]
    projects = {p for r in chain.reaction_ids if (p := reactions[r].project)}
    heading = (
        f"Campaign chaining {len(chain.reaction_ids)} experiments (product → reactant linkage)."
        if chain.ordered
        else (
            f"Campaign of {len(chain.reaction_ids)} interlinked experiments "
            f"(contains a cycle — the listing is not a causal sequence)."
        )
    )
    label = "Steps" if chain.ordered else "Members"
    body = (
        f"{heading}\n\n"
        f"{label}:\n" + "\n".join(steps) + "\n\n"
        "Handoffs:\n" + "\n".join(handoffs) + "\n"
    )
    return Note(
        id=_campaign_id(chain.reaction_ids),
        type="campaign",
        created_by="agent",
        source="memory:chain-detection",
        tags=sorted(projects),
        body=body,
    )
