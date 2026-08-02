"""Tests for optimization-campaign grouping + note + job (plan Phase 5, episodic).

Proves same-transformation runs are grouped by DRFP similarity, that a singleton is not a
campaign, that the note lays the runs out comparably with citations, and that the job PR-gates
one note per campaign. Also covers the shared clustering helper. All in-memory (no store, no
git).
"""

import asyncio

from chemclaw.ingest.eln.ord import Component, Impurity, OrdReaction, Role
from chemclaw.memory.jobs import synthesize_optimization_campaigns
from chemclaw.memory.optimization import (
    OptimizationCampaign,
    find_optimization_campaigns,
    optimization_campaign_note,
)
from chemclaw.memory.similarity import cluster_by_similarity, reaction_fingerprints
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
        assert submitter.submissions[0].files[0].path.startswith("knowledge/optimization-campaign/")

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


# --- the outcome-quality columns ------------------------------------------------------


def _headers(body: str) -> list[str]:
    """The campaign table's column headers, as a reader of the rendered note would see them."""
    header = next(line for line in body.splitlines() if line.startswith("| Run |"))
    return [cell.strip() for cell in header.strip("|").split("|")]


def _row(body: str, reaction_id: str) -> list[str]:
    """One run's rendered row cells."""
    line = next(
        line for line in body.splitlines() if line.startswith(f"| [[reaction-{reaction_id}]] |")
    )
    return [cell.strip() for cell in line.strip("|").split("|")]


def _note(runs: list[OrdReaction]) -> str:
    """The campaign note body for these runs — the artifact built for side-by-side reading."""
    return optimization_campaign_note(
        "optimization-abc",
        OptimizationCampaign(reaction_ids=[r.reaction_id for r in runs]),
        {r.reaction_id: r for r in runs},
    ).body


def test_the_table_compares_purity_and_the_impurity_profile() -> None:
    """A process campaign optimizes the impurity yield hides; the table has to carry it.

    Yield alone cannot separate these two runs — the second is worse on yield and far better on
    the impurity that decides whether the batch is shippable.
    """
    runs = [
        _ester("run-1", 80, 85).model_copy(
            update={
                "purity_percent": 91.0,
                "impurities": [Impurity(name="des-ethyl", area_percent=6.2)],
            }
        ),
        _ester("run-2", 60, 78).model_copy(
            update={
                "purity_percent": 99.1,
                "impurities": [Impurity(name="des-ethyl", area_percent=0.3)],
            }
        ),
    ]
    body = _note(runs)
    assert _headers(body) == [
        "Run",
        "Performed",
        "Temp (°C)",
        "Time (h)",
        "Yield (%)",
        "Purity (%)",
        "Major impurity",
        "Impurity area (%)",
        "Changed vs previous",
    ]
    assert _row(body, "run-1")[5:8] == ["91", "des-ethyl", "6.2"]
    assert _row(body, "run-2")[5:8] == ["99.1", "des-ethyl", "0.3"]


def test_a_campaign_that_recorded_no_quality_data_keeps_a_clean_table() -> None:
    """Sparsity is handled by dropping the column, not by a row of dashes or of "None".

    A column nobody filled costs width in every row and invites the reader to conclude the
    impurity was measured and found absent. When no run in the campaign recorded any of the three,
    the table is exactly the one it was before they existed.
    """
    body = _note([_ester("run-1", 80, 85), _ester("run-2", 100, 92)])
    assert _headers(body) == [
        "Run",
        "Performed",
        "Temp (°C)",
        "Time (h)",
        "Yield (%)",
        "Changed vs previous",
    ]
    assert len(_row(body, "run-1")) == 6
    assert "None" not in body


def test_a_column_survives_for_the_one_run_that_recorded_it() -> None:
    """Partial data keeps the column: a run with no number reads as "not measured here"."""
    runs = [
        _ester("run-1", 80, 85).model_copy(update={"purity_percent": 99.4}),
        _ester("run-2", 100, 92),
    ]
    body = _note(runs)
    assert "Purity (%)" in _headers(body)
    assert "Major impurity" not in _headers(body)
    assert _row(body, "run-1")[5] == "99.4"
    assert _row(body, "run-2")[5] == "—"


def test_the_major_impurity_is_the_largest_by_area_not_the_first_listed() -> None:
    """The one a chemist chases, not the one the export happened to print first."""
    runs = [
        _ester("run-1", 80, 85).model_copy(
            update={
                "impurities": [
                    Impurity(name="RRT 0.71", area_percent=0.4),
                    Impurity(name="des-ethyl", area_percent=5.8),
                ]
            }
        ),
        _ester("run-2", 100, 92),
    ]
    body = _note(runs)
    assert _row(body, "run-1")[5:7] == ["des-ethyl", "5.8"]


def test_several_unranked_impurities_name_no_major_one() -> None:
    """With no area% the list is unranked, and naming a "major" impurity would be a fabrication.

    A single recorded impurity is the exception that needs no ranking — the record names one, so
    calling it the major one adds no claim.
    """
    two_unranked = _ester("run-1", 80, 85).model_copy(
        update={"impurities": [Impurity(name="RRT 0.71"), Impurity(name="RRT 1.24")]}
    )
    lone = _ester("run-2", 100, 92).model_copy(update={"impurities": [Impurity(name="des-ethyl")]})
    body = _note([two_unranked, lone])
    assert _row(body, "run-1")[5] == "—"
    assert _row(body, "run-2")[5] == "des-ethyl"
    assert "Impurity area (%)" not in _headers(body)  # nobody recorded one
