"""Tests for optimization-campaign grouping + note + job (plan Phase 5, episodic).

Proves same-transformation runs are grouped by DRFP similarity, that a singleton is not a
campaign, that the note lays the runs out comparably with citations, and that the job PR-gates
one note per campaign. Also covers the shared clustering helper. All in-memory (no store, no
git).
"""

import asyncio
import re
from datetime import date

from chemclaw.ingest.eln.ord import Component, Impurity, OrdReaction, Role
from chemclaw.kg.pr_gate import propose_note
from chemclaw.memory.jobs import build_optimization_notes
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
    # Temp then yield, with no Time column at all: nothing in this campaign recorded a time, and a
    # column of dashes is what `drop_empty_columns` exists to remove.
    assert "| 80 | 85 |" in note.body
    assert "diethyl ether impurity" in note.body  # process/observation detail is surfaced
    assert set(note.outgoing_links()) == {"reaction-run-1", "reaction-run-2"}


def test_job_pr_gates_one_note_per_campaign() -> None:
    """The optimization job proposes exactly one note per detected campaign.

    Driven as the durable job drives it — `build_optimization_notes`, then one PR-gate proposal per
    note — rather than through the whole-batch `synthesize_optimization_campaigns` wrapper, which
    nothing in `src/` had called since F10-D2 and which is now gone.
    """

    async def _run() -> None:
        reactions = [_ester("run-1", 80, 85), _ester("run-2", 100, 92), _suzuki()]
        submitter = FakeSubmitter()
        refs = [await propose_note(note, submitter) for note in build_optimization_notes(reactions)]
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


def _cells(line: str) -> list[str]:
    r"""Split a rendered row into cells the way a Markdown reader does.

    On *unescaped* pipes only: `render_table` escapes a `|` inside a value, and a reader sees
    `des-ethyl \| 99.9` as one cell. Splitting on every pipe would count an escaped one as a column
    boundary, which is exactly the misreading the escaping exists to prevent.
    """
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip("|"))]


def _headers(body: str) -> list[str]:
    """The campaign table's column headers, as a reader of the rendered note would see them."""
    return _cells(next(line for line in body.splitlines() if line.startswith("| Run |")))


def _row(body: str, reaction_id: str) -> list[str]:
    """One run's rendered row cells."""
    start = f"| [[reaction-{reaction_id}]] |"
    return _cells(next(line for line in body.splitlines() if line.startswith(start)))


def _cell(body: str, reaction_id: str, header: str) -> str:
    """One run's cell under a named column.

    Addressed by header rather than by index, because which columns exist is now a property of what
    the campaign recorded: since every column but `Run` and `Changed vs previous` goes through
    `drop_empty_columns`, a fixture that records no time shifts every position after it. A
    positional assertion in that world tests the column layout while claiming to test a value.
    """
    headers = _headers(body)
    assert header in headers, f"{header!r} is not a column of this table: {headers}"
    return _row(body, reaction_id)[headers.index(header)]


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
    # No run in this fixture carries a date or a time, so neither column appears — the same
    # `drop_empty_columns` rule the quality columns have always been under, now applied to the
    # setpoints too. The ordering caveat above the table is what says the runs are undated.
    assert _headers(body) == [
        "Run",
        "Temp (°C)",
        "Yield (%)",
        "Purity (%)",
        "Major impurity",
        "Impurity area (%)",
        "Changed vs previous",
    ]
    for run, purity, area in (("run-1", "91", "6.2"), ("run-2", "99.1", "0.3")):
        assert _cell(body, run, "Purity (%)") == purity
        assert _cell(body, run, "Major impurity") == "des-ethyl"
        assert _cell(body, run, "Impurity area (%)") == area


def test_a_campaign_that_recorded_no_quality_data_keeps_a_clean_table() -> None:
    """Sparsity is handled by dropping the column, not by a row of dashes or of "None".

    A column nobody filled costs width in every row and invites the reader to conclude the
    impurity was measured and found absent. When no run in the campaign recorded any of the three,
    the table is exactly the one it was before they existed.
    """
    body = _note([_ester("run-1", 80, 85), _ester("run-2", 100, 92)])
    assert _headers(body) == ["Run", "Temp (°C)", "Yield (%)", "Changed vs previous"]
    assert len(_row(body, "run-1")) == 4
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
    assert _cell(body, "run-1", "Purity (%)") == "99.4"
    assert _cell(body, "run-2", "Purity (%)") == "—"


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
    assert _cell(body, "run-1", "Major impurity") == "des-ethyl"
    assert _cell(body, "run-1", "Impurity area (%)") == "5.8"


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
    assert _cell(body, "run-1", "Major impurity") == "—"
    assert _cell(body, "run-2", "Major impurity") == "des-ethyl"
    assert "Impurity area (%)" not in _headers(body)  # nobody recorded one


def test_an_impurity_name_cannot_add_a_column_to_the_campaign_table() -> None:
    """The campaign note reaches the shared renderer with ELN free text, exactly as the digest does.

    An impurity name is whatever the source instrument or analyst typed, and it lands in a cell. A
    `|` in it does not render badly — it renders as another column, silently shifting every value
    after it under the wrong heading, which in this artifact means reading one run's impurity area
    as another run's yield. The fix is in `memory.comparison.render_table` rather than at either
    caller, and this is the second caller proving it.
    """
    runs = [
        _ester("run-1", 80, 85).model_copy(
            update={
                "purity_percent": 91.0,
                "impurities": [Impurity(name="des-ethyl | 99.9", area_percent=6.2)],
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

    assert len(_row(body, "run-1")) == len(_headers(body)), (
        "the impurity name added a column, so every cell after it reads under the wrong heading"
    )
    assert [
        _cell(body, "run-1", h) for h in ("Purity (%)", "Major impurity", "Impurity area (%)")
    ] == ["91", r"des-ethyl \| 99.9", "6.2"], (
        "the name is evidence and must survive, escaped rather than dropped"
    )


def test_a_partly_dated_campaign_keeps_its_date_column() -> None:
    """The rule is emptiness, not datedness: one recorded value keeps the column for every row.

    The complement of the setpoint columns disappearing on a prose-only ELN. A campaign where *some*
    runs carry a date is exactly the case a chemist must be able to read — the dated ones are a
    trajectory and the undated ones are parked at the end — so the column stays and the caveat above
    the table names the runs with no place in time.
    """
    dated = _ester("run-1", 80, 85).model_copy(update={"performed_at": date(2026, 5, 4)})
    undated = _ester("run-2", 100, 92)
    body = _note([dated, undated])

    assert "Performed" in _headers(body)
    assert _cell(body, "run-1", "Performed") == "2026-05-04"
    assert _cell(body, "run-2", "Performed") == "—"
    assert "[[reaction-run-2]]" in body.split("|")[0], "the caveat names the undated run"
