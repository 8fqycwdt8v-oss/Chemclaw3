"""Tests for optimization-campaign grouping + note + job (plan Phase 5, episodic).

Proves same-transformation runs are grouped by DRFP similarity, that a singleton is not a
campaign, that the note lays the runs out comparably with citations, and that the job PR-gates
one note per campaign. Also covers the shared clustering helper. All in-memory (no store, no
git).
"""

import asyncio

from eln.ord import Component, OrdReaction, Role
from memory.jobs import synthesize_optimization_campaigns
from memory.optimization import (
    OptimizationCampaign,
    find_optimization_campaigns,
    optimization_campaign_note,
)
from memory.similarity import cluster_by_similarity, reaction_fingerprints
from tests.conftest import FakeSubmitter


def _ester(
    reaction_id: str, temperature: float, yield_pct: float, procedure: str = ""
) -> OrdReaction:
    """A run of one esterification, varying only the conditions/outcome (same transformation)."""
    return OrdReaction(
        reaction_id=reaction_id,
        inputs=[
            Component(smiles="CCO", role=Role.REACTANT),
            Component(smiles="CC(=O)O", role=Role.REACTANT),
        ],
        outcomes=[Component(smiles="CCOC(C)=O", role=Role.PRODUCT)],
        temperature_c=temperature,
        yield_percent=yield_pct,
        provenance="eln:chemist-a",
        procedure_text=procedure or None,
    )


def _suzuki() -> OrdReaction:
    """A structurally different reaction that must not join the esterification campaign."""
    return OrdReaction(
        reaction_id="suzuki-1",
        inputs=[
            Component(smiles="OB(O)c1ccccc1", role=Role.REACTANT),
            Component(smiles="Brc1ccccc1", role=Role.REACTANT),
        ],
        outcomes=[Component(smiles="c1ccc(-c2ccccc2)cc1", role=Role.PRODUCT)],
        provenance="eln:chemist-b",
    )


def test_groups_same_transformation_runs() -> None:
    """Two runs of one transformation group; an unrelated reaction stays out."""
    reactions = [_ester("run-1", 80, 85), _ester("run-2", 100, 92), _suzuki()]
    campaigns = find_optimization_campaigns(reactions)
    assert len(campaigns) == 1
    assert campaigns[0].reaction_ids == ["run-1", "run-2"]


def test_singleton_is_not_a_campaign() -> None:
    """A transformation run only once is not an optimization campaign (nothing to compare)."""
    assert find_optimization_campaigns([_ester("run-1", 80, 85), _suzuki()]) == []


def test_note_lays_out_runs_with_citations() -> None:
    """The note renders a comparative table, cites each run, and shows a procedure excerpt."""
    reactions = {
        "run-1": _ester("run-1", 80, 85, "Stirred at 80 C; some diethyl ether impurity observed."),
        "run-2": _ester("run-2", 100, 92),
    }
    note = optimization_campaign_note(
        "optimization-abc", OptimizationCampaign(reaction_ids=["run-1", "run-2"]), reactions
    )
    assert note.type == "optimization-campaign"
    assert note.created_by == "agent"
    assert "[[reaction-run-1]]" in note.body and "[[reaction-run-2]]" in note.body
    assert "| 80 | — | 85 |" in note.body  # run-1 row: temp, (no time), yield
    assert "diethyl ether impurity" in note.body  # process/observation detail is surfaced
    assert set(note.outgoing_links()) == {"reaction-run-1", "reaction-run-2"}


def test_job_pr_gates_one_note_per_campaign() -> None:
    """synthesize_optimization_campaigns proposes exactly one note per detected campaign."""

    async def _run() -> None:
        reactions = [_ester("run-1", 80, 85), _ester("run-2", 100, 92), _suzuki()]
        submitter = FakeSubmitter()
        refs = await synthesize_optimization_campaigns(reactions, submitter)
        assert len(refs) == 1
        assert len(submitter.submissions) == 1
        assert submitter.submissions[0].path.startswith("knowledge/optimization-campaign/")

    asyncio.run(_run())


def test_clustering_drops_degenerate_reactions() -> None:
    """A degenerate reaction (no computable fingerprint) is dropped, never fatal (G4)."""
    degenerate = OrdReaction(
        reaction_id="degenerate",
        inputs=[Component(smiles="CCO", role=Role.REACTANT)],
        outcomes=[Component(smiles="CCO", role=Role.PRODUCT)],
        provenance="p",
    )
    fingerprints = reaction_fingerprints([_ester("run-1", 80, 85), degenerate])
    assert "degenerate" not in fingerprints
    assert cluster_by_similarity(fingerprints, 0.7) == [["run-1"]]
