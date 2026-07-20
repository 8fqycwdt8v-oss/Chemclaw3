"""Behavioral tests for the memory layers (plan Phase 5), runnable without a server.

Proves the CHECKMATE 5 acceptance: chained experiments become a `campaign` note that cites
each member reaction, and reactions recurring across >=2 projects become a `playbook`
candidate + note with mandatory evidence — all from existing pieces (fingerprint identity,
the reaction schema, the note model), no new infrastructure.
"""

import asyncio

import pytest

from eln.ord import Component, OrdReaction, Role
from kg.pr_gate import NoteSubmission
from memory.campaign import campaign_note_from_chain
from memory.chains import detect_chains
from memory.interaction import note_from_confirmed_answer
from memory.jobs import distill_playbooks, synthesize_campaigns
from memory.playbook import (
    PlaybookError,
    find_playbook_candidates,
    playbook_note,
)


class _FakeSubmitter:
    """Captures proposed notes instead of pushing a git branch."""

    def __init__(self) -> None:
        self.notes: list[NoteSubmission] = []

    async def submit(self, submission: NoteSubmission) -> str:
        self.notes.append(submission)
        return f"pr://{submission.branch}"


def _reaction(
    rid: str, reactants: list[str], products: list[str], project: str | None = None
) -> OrdReaction:
    """A minimal reaction from reactant/product SMILES lists."""
    return OrdReaction(
        reaction_id=rid,
        inputs=[Component(smiles=s, role=Role.REACTANT) for s in reactants],
        outcomes=[Component(smiles=s, role=Role.PRODUCT) for s in products],
        provenance="test",
        project=project,
    )


# --- chain detection (5.2) ------------------------------------------------------------


def test_detect_chain_links_product_to_reactant() -> None:
    """A→B when a product of A is a reactant of B; the linked pair is one ordered chain."""
    a = _reaction("a", ["CCO"], ["CC=O"])  # ethanol → acetaldehyde
    b = _reaction("b", ["CC=O", "O"], ["CC(O)O"])  # acetaldehyde → ...
    chains = detect_chains([b, a])  # order-independent input
    assert len(chains) == 1
    assert chains[0].reaction_ids == ["a", "b"]  # topological: producer before consumer
    assert chains[0].links[0].from_reaction == "a"
    assert chains[0].links[0].to_reaction == "b"


def test_unlinked_reactions_are_not_a_chain() -> None:
    """Reactions that share no product↔reactant compound form no campaign."""
    a = _reaction("a", ["CCO"], ["CC=O"])
    b = _reaction("b", ["c1ccccc1"], ["Brc1ccccc1"])  # unrelated
    assert detect_chains([a, b]) == []


def test_reagent_match_does_not_chain() -> None:
    """Only true reactant inputs link a chain — a shared solvent/reagent does not."""
    a = _reaction("a", ["CCO"], ["O"])  # product water
    b = OrdReaction(
        reaction_id="b",
        inputs=[
            Component(smiles="CCO", role=Role.REACTANT),
            Component(smiles="O", role=Role.SOLVENT),  # water only as solvent
        ],
        outcomes=[Component(smiles="CC=O", role=Role.PRODUCT)],
        provenance="test",
    )
    assert detect_chains([a, b]) == []  # water is a's product but only b's solvent


def test_cycle_is_flagged_not_a_false_sequence() -> None:
    """A reversible pair (A→B and B→A) is a chain marked unordered, not a fake causal order."""
    a = _reaction("a", ["CCO"], ["CC=O"])
    b = _reaction("b", ["CC=O"], ["CCO"])  # reverses a
    chains = detect_chains([a, b])
    assert len(chains) == 1
    assert chains[0].ordered is False  # cycle → not a topological sequence
    note = campaign_note_from_chain(chains[0], {"a": a, "b": b})
    assert "cycle" in note.body.lower()  # the note is honest about it


# --- campaign note (5.1/5.3) ----------------------------------------------------------


def test_campaign_note_cites_each_member() -> None:
    """The campaign note links every chained reaction (real back-references) + carries project."""
    a = _reaction("a", ["CCO"], ["CC=O"], project="proj-x")
    b = _reaction("b", ["CC=O", "O"], ["CC(O)O"], project="proj-x")
    chain = detect_chains([a, b])[0]
    note = campaign_note_from_chain(chain, {"a": a, "b": b})
    assert note.type == "campaign"
    assert note.created_by == "agent"
    assert note.outgoing_links() == ["reaction-a", "reaction-b"]  # cites both members
    assert note.tags == ["proj-x"]
    assert note.id.startswith("campaign-")


def test_campaign_id_is_stable() -> None:
    """The campaign id is derived from its members, so re-synthesis is idempotent."""
    a = _reaction("a", ["CCO"], ["CC=O"])
    b = _reaction("b", ["CC=O"], ["CC(O)O"])
    chain = detect_chains([a, b])[0]
    first = campaign_note_from_chain(chain, {"a": a, "b": b}).id
    second = campaign_note_from_chain(chain, {"a": a, "b": b}).id
    assert first == second


# --- playbook (5.4) -------------------------------------------------------------------


def test_playbook_candidate_needs_two_projects() -> None:
    """Similar reactions recur into a playbook candidate only across >=2 projects."""
    ester_x = _reaction("x", ["CCO", "CC(=O)O"], ["CCOC(C)=O"], project="proj-x")
    ester_y = _reaction("y", ["CCCO", "CC(=O)O"], ["CCCOC(C)=O"], project="proj-y")
    ester_x2 = _reaction("x2", ["CCCCO", "CC(=O)O"], ["CCCCOC(C)=O"], project="proj-x")

    # Two projects (x, y) → a candidate.
    candidates = find_playbook_candidates([ester_x, ester_y, ester_x2], threshold=0.3)
    assert len(candidates) == 1
    assert candidates[0].projects == ["proj-x", "proj-y"]
    assert set(candidates[0].reaction_ids) >= {"x", "y"}


def test_single_project_repetition_is_not_a_playbook() -> None:
    """Repetition within one project is episodic, not a transferable playbook."""
    a = _reaction("a", ["CCO", "CC(=O)O"], ["CCOC(C)=O"], project="proj-x")
    b = _reaction("b", ["CCCO", "CC(=O)O"], ["CCCOC(C)=O"], project="proj-x")
    assert find_playbook_candidates([a, b], threshold=0.3) == []


def test_degenerate_reaction_does_not_abort_distillation() -> None:
    """A reaction whose fingerprint is degenerate is skipped, not fatal to the whole job (G4)."""
    ester_x = _reaction("x", ["CCO", "CC(=O)O"], ["CCOC(C)=O"], project="proj-x")
    ester_y = _reaction("y", ["CCCO", "CC(=O)O"], ["CCCOC(C)=O"], project="proj-y")
    degenerate = _reaction("bad", ["C"], ["C"], project="proj-z")  # empty DRFP → FingerprintError
    candidates = find_playbook_candidates([ester_x, degenerate, ester_y], threshold=0.3)
    assert len(candidates) == 1  # the good cross-project pair still surfaces
    assert "bad" not in candidates[0].reaction_ids


def test_playbook_note_requires_evidence() -> None:
    """A playbook with citations builds; one without is rejected (Belegverweise verpflichtend)."""
    note = playbook_note("esterification", "Fischer esterification recurs.", ["x", "y"])
    assert note.type == "playbook"
    assert note.outgoing_links() == ["reaction-x", "reaction-y"]  # mandatory evidence
    with pytest.raises(PlaybookError, match="no evidence"):
        playbook_note("empty", "no evidence here", [])


# --- jobs (5.3/5.4 wiring) ------------------------------------------------------------


def test_synthesize_campaigns_proposes_notes_via_pr_gate() -> None:
    """The campaign job proposes one PR-gated campaign note per detected chain."""
    a = _reaction("a", ["CCO"], ["CC=O"], project="proj-x")
    b = _reaction("b", ["CC=O"], ["CC(O)O"], project="proj-x")
    sub = _FakeSubmitter()
    refs = asyncio.run(synthesize_campaigns([a, b], sub))
    assert len(refs) == 1
    assert sub.notes[0].path.startswith("knowledge/campaign/campaign-")


def test_distill_playbooks_proposes_evidence_backed_notes() -> None:
    """The playbook job proposes a cross-project playbook note citing its evidence."""
    ester_x = _reaction("x", ["CCO", "CC(=O)O"], ["CCOC(C)=O"], project="proj-x")
    ester_y = _reaction("y", ["CCCO", "CC(=O)O"], ["CCCOC(C)=O"], project="proj-y")
    sub = _FakeSubmitter()
    refs = asyncio.run(distill_playbooks([ester_x, ester_y], sub))
    assert len(refs) == 1
    assert sub.notes[0].path.startswith("knowledge/playbook/playbook-")
    assert "proj-x" in sub.notes[0].content and "proj-y" in sub.notes[0].content


# --- user interaction (5.5) -----------------------------------------------------------


def test_interaction_note_captures_confirmed_answer() -> None:
    """A confirmed user answer becomes an episodic `interaction` note citing its evidence."""
    note = note_from_confirmed_answer(
        "q-42",
        "Best solvent for the coupling?",
        "Aqueous dioxane worked at 90 °C.",
        ["reaction-eln-2026-002"],
    )
    assert note.type == "interaction"
    assert note.created_by == "agent"  # still PR-gated before it is trusted
    assert "confirmed" in note.body.lower()
    assert note.outgoing_links() == ["reaction-eln-2026-002"]
