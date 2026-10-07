"""Build a `campaign` note from a detected chain.

The episodic memory note narrates a chain of experiments and cites every one via a
`[[reaction-<id>]]` wikilink. Those ids resolve outside the markdown graph
(`kg.note.EXTERNAL_ID_PREFIXES`) against `reaction_records`, and `kg.validate` checks them. This
builds the factual skeleton (transformation sequence and evidence); a richer narrative is the
on-demand `campaign-narrative-synthesis` skill's job, and nothing applies it automatically.
"""

from datetime import date

from chemclaw.ingest.eln.ord import OrdReaction
from chemclaw.kg.note import Note
from chemclaw.memory.chains import Chain
from chemclaw.memory.ids import stable_id


def campaign_note_from_chain(
    chain: Chain, reactions: dict[str, OrdReaction], *, minted_on: date | None = None
) -> Note:
    """Map a chain to an agent `campaign` note that links to each member reaction.

    `reactions` maps reaction id to its `OrdReaction` for per-step SMILES. The project, if members
    share one, is carried so the semantic layer can group campaigns across projects.

    `minted_on` becomes `valid_from`: the date of the anchor run (`min(reaction_ids)`), the same id
    the note id is keyed on, so the date stays fixed as the cluster grows. Without it the note is
    open-ended, which the digest reads as "not news".
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
        id=stable_id("campaign", chain.reaction_ids),
        type="campaign",
        created_by="agent",
        source="memory:chain-detection",
        tags=sorted(projects),
        body=body,
        valid_from=minted_on,
    )
