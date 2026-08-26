"""Tests for reading an optimization series as a sequence (D-162).

The behaviour under test is what a technician working one step for weeks needs the system to
see: the runs in the order they were performed, what changed at each step, what the record does
*not* license (a trajectory over undated runs, a motive behind a change), and the hypothesis the
run was testing surviving ingestion. In-memory throughout — no store, no git, no LLM.
"""

from datetime import date

from chemclaw.ingest.eln.ord import Component, OrdReaction, Role, StepKind
from chemclaw.ingest.eln.ord import ReactionStep as Step
from chemclaw.ingest.eln.record import record_from_ord_reaction
from chemclaw.memory.optimization import OptimizationCampaign, optimization_campaign_note
from chemclaw.memory.progression import (
    changes_between,
    order_chronologically,
    progression,
    text_change,
)


def _run(
    reaction_id: str,
    *,
    day: int | None = None,
    temperature: float | None = 80.0,
    time_h: float | None = 4.0,
    solvent: str | None = "CN(C)C=O",  # DMF
    yield_pct: float | None = None,
    hypothesis: str | None = None,
    steps: list[Step] | None = None,
) -> OrdReaction:
    """One run of the same esterification, varying only what a test is about."""
    inputs = [
        Component(smiles="CCO", role=Role.REACTANT),
        Component(smiles="CC(=O)O", role=Role.REACTANT),
    ]
    if solvent is not None:
        inputs.append(Component(smiles=solvent, role=Role.SOLVENT))
    return OrdReaction(
        reaction_id=reaction_id,
        inputs=inputs,
        outcomes=[Component(smiles="CCOC(C)=O", role=Role.PRODUCT)],
        temperature_c=temperature,
        time_h=time_h,
        yield_percent=yield_pct,
        performed_at=None if day is None else date(2026, 3, day),
        provenance="eln:chemist-a",
        hypothesis=hypothesis,
        steps=steps or [],
    )


# --- ordering -------------------------------------------------------------------------


def test_runs_are_ordered_by_the_day_they_were_performed() -> None:
    """The point of the module: id order is not run order, and run order is what a series is."""
    runs = [_run("z-first", day=1), _run("a-second", day=5), _run("m-third", day=9)]
    assert [r.reaction_id for r in order_chronologically(runs)] == [
        "z-first",
        "a-second",
        "m-third",
    ]


def test_undated_runs_sort_last_not_first() -> None:
    """An unknown date is not "long ago" — parking it at the end keeps the dated prefix clean."""
    ordered = order_chronologically([_run("undated"), _run("dated", day=2)])
    assert [r.reaction_id for r in ordered] == ["dated", "undated"]


def test_same_day_runs_fall_back_to_a_stable_id_order() -> None:
    """Two runs on one day must not reorder between syntheses, or every re-run is a fake diff."""
    runs = [_run("b", day=3), _run("a", day=3)]
    assert [r.reaction_id for r in order_chronologically(runs)] == ["a", "b"]
    assert [r.reaction_id for r in order_chronologically(list(reversed(runs)))] == ["a", "b"]


# --- what changed between two runs ----------------------------------------------------


def test_a_setpoint_change_is_named_with_its_units() -> None:
    """The delta a reader needs is "what moved, from what, to what" — not two condition lists."""
    changes = changes_between(_run("a", temperature=80), _run("b", temperature=60))
    assert [c.describe() for c in changes] == ["temperature 80 °C → 60 °C"]


def test_a_setpoint_one_run_did_not_record_is_not_a_change() -> None:
    """A missing number is a gap in the *record*, so diffing against it invents a change.

    This test asserted the opposite until 2026-08-26 — `time 4 h → —` — which is the defect
    `agent/condense._changes` had already been fixed for and `progression` had not
    (`BACKLOG.md` §2). Absent-to-present is a difference in what someone wrote down, never in what
    they did, and the two are indistinguishable to a reader once they are in the same column of
    `optimization_campaign_note`. One rule now, `both_recorded`, applied inside `number_change` and
    `text_change` rather than guarded separately at each call site.

    A *recorded* zero still compares: it is a setpoint, not a gap.
    """
    assert changes_between(_run("a", time_h=4), _run("b", time_h=None)) == []
    assert changes_between(_run("a", time_h=None), _run("b", time_h=4)) == []
    assert [
        c.describe() for c in changes_between(_run("a", temperature=0), _run("b", temperature=25))
    ] == ["temperature 0 °C → 25 °C"]


def test_a_species_set_is_diffed_even_when_one_side_is_empty() -> None:
    """The asymmetry `both_recorded` is drawn around, pinned so it cannot be "unified" by mistake.

    A setpoint is an optional scalar and `None` means nobody wrote it down. A role's species set is
    derived from a components list that is present either way, so an empty `reagent` set beside a
    full one is the record stating the run used no reagent — a real change, and the most common one
    a series carries. Applying the absence rule to both would have traded a rare fabrication (a
    partially transcribed source) for a routine erasure.
    """
    changes = changes_between(_run("a", solvent=None), _run("b"))
    assert [c.describe() for c in changes] == ["solvent — → N,N-dimethylformamide"]


def test_a_solvent_swap_names_only_what_went_out_and_what_came_in() -> None:
    """Reported as the swap, not as both full sets — and by name where the table knows one."""
    changes = changes_between(
        _run("a", solvent="CN(C)C=O"),  # DMF
        _run("b", solvent="C1CCOC1"),  # THF
    )
    assert [c.describe() for c in changes] == ["solvent N,N-dimethylformamide → tetrahydrofuran"]


def test_the_same_solvent_spelled_differently_is_not_a_change() -> None:
    """Identity is structural, so a source's SMILES spelling cannot fabricate a delta."""
    assert changes_between(_run("a", solvent="CN(C)C=O"), _run("b", solvent="O=CN(C)C")) == []


def test_a_reagent_added_mid_procedure_is_diffed_too() -> None:
    """Swapping a reagent added in step 3 is exactly the kind of change a series is made of."""
    with_acid = _run(
        "b",
        steps=[
            Step(
                index=1,
                kind=StepKind.ADDITION,
                text="Add H2SO4",
                components=[Component(smiles="OS(=O)(=O)O", role=Role.REAGENT)],
            )
        ],
    )
    changes = changes_between(_run("a"), with_acid)
    assert [c.variable for c in changes] == ["reagent"]
    assert changes[0].before == "—"


def test_identical_runs_report_no_change() -> None:
    """A repeat is a reproducibility check; inventing a delta for it would be a lie."""
    assert changes_between(_run("a"), _run("b")) == []


# --- the series -----------------------------------------------------------------------


def test_each_run_is_diffed_against_the_one_before_it_in_time() -> None:
    """Not against the first run, and not against id-order neighbours: against yesterday's."""
    series = progression(
        [
            _run("c", day=9, temperature=60),
            _run("a", day=1, temperature=100),
            _run("b", day=5, temperature=80),
        ]
    )
    assert [step.reaction_id for step in series.steps] == ["a", "b", "c"]
    assert series.steps[0].changes == []  # nothing precedes the first run
    assert [c.describe() for c in series.steps[1].changes] == ["temperature 100 °C → 80 °C"]
    assert [c.describe() for c in series.steps[2].changes] == ["temperature 80 °C → 60 °C"]


def test_a_series_is_a_timeline_only_when_every_run_is_dated() -> None:
    """The honesty check: one undated run means the order is not fully evidenced."""
    assert progression([_run("a", day=1), _run("b", day=2)]).is_timeline()
    partial = progression([_run("a", day=1), _run("b")])
    assert not partial.is_timeline()
    assert partial.undated() == ["b"]


# --- the campaign note ----------------------------------------------------------------


def _note(runs: list[OrdReaction]) -> str:
    """Render the campaign note body for these runs (the artifact the agent actually reads)."""
    return optimization_campaign_note(
        "opt-test",
        OptimizationCampaign(reaction_ids=sorted(r.reaction_id for r in runs)),
        {r.reaction_id: r for r in runs},
    ).body


def test_the_note_lays_the_runs_out_in_time_order_with_their_deltas() -> None:
    """The whole point: six weeks of work readable as a progression, not as a set."""
    body = _note(
        [
            _run("day-9", day=9, temperature=60, yield_pct=88),
            _run("day-1", day=1, temperature=100, yield_pct=71),
            _run("day-5", day=5, temperature=80, yield_pct=85),
        ]
    )
    rows = [line for line in body.splitlines() if line.startswith("| [[reaction-")]
    assert [row.split("|")[1].strip() for row in rows] == [
        "[[reaction-day-1]]",
        "[[reaction-day-5]]",
        "[[reaction-day-9]]",
    ]
    assert "2026-03-01" in rows[0]
    assert "first run" in rows[0]
    assert "temperature 100 °C → 80 °C" in rows[1]
    assert "Runs in the order they were performed." in body


def test_the_note_refuses_to_imply_a_sequence_it_cannot_evidence() -> None:
    """With no dates the rows are an id listing, and the note has to say so."""
    body = _note([_run("a"), _run("b", temperature=60)])
    assert "**No run carries a date**" in body
    assert "not evidence of what was tried next" in body


def test_the_note_names_the_undated_runs_when_only_some_are_dated() -> None:
    """A partial timeline is still a timeline — as long as the exceptions are visible."""
    body = _note([_run("dated", day=2), _run("floating")])
    assert "1 with no recorded date, listed last: [[reaction-floating]]" in body


def test_a_repeated_run_is_marked_as_a_repeat_not_left_blank() -> None:
    """Saying unchanged is information; an empty cell reads as a gap in the record instead."""
    body = _note([_run("a", day=1), _run("b", day=2)])
    assert "unchanged (repeat)" in body


def test_the_note_carries_what_each_run_was_testing() -> None:
    """The intent is the question every condition in the row is an answer to."""
    body = _note(
        [
            _run("a", day=1, hypothesis="baseline at reflux"),
            _run("b", day=2, temperature=60, hypothesis="does 60 °C keep the yield?"),
        ]
    )
    assert "- tested: does 60 °C keep the yield?" in body


# --- ingestion ------------------------------------------------------------------------


def test_the_hypothesis_survives_into_the_reaction_note() -> None:
    """Leading with it, because it is what makes the run legible to a later reader."""
    body = record_from_ord_reaction(_run("a", day=1, hypothesis="is the impurity thermal?")).body
    assert "Tested: is the impurity thermal?" in body


def test_a_run_with_no_recorded_hypothesis_says_nothing_about_one() -> None:
    """Silence is "not recorded", never "there was no hypothesis"."""
    assert "Tested:" not in record_from_ord_reaction(_run("a", day=1)).body


def test_two_spellings_of_one_solvent_are_not_reported_as_a_change() -> None:
    """The one comparison that works on solvent *names* could not tell two names apart.

    `canonical_condition` folds `DMF`, `N,N-dimethylformamide` and `CN(C)C=O` to one token through
    `core.reagents`, and its docstring says why: without it "an optimization campaign could be
    split in two by spelling alone". It had no caller in `src/` at all — kept alive by a test that
    called it directly, which is the `reject_widening` / `map_to_hpc_identity` shape `CLAUDE.md`
    names. Meanwhile `text_change`, the comparison a chemist actually reads in the turn-time
    "Changed vs previous" column, compared casefolded prose:

        'DMF' vs 'N,N-dimethylformamide':
            canonical_condition folds -> True
            text_change reports       -> solvent DMF → N,N-dimethylformamide

    A fabricated lever, in the artifact built for reading levers off. What is *displayed* is still
    what was written; only the decision about whether anything moved is folded.
    """
    for before, after in (
        ("DMF", "N,N-dimethylformamide"),
        ("DIPEA", "N,N-diisopropylethylamine"),
        ("DMF", "CN(C)C=O"),
    ):
        assert text_change("solvent", before, after) is None, (
            f"{before!r} → {after!r} is one species written twice, not a swap"
        )

    real = text_change("solvent", "DMF", "2-MeTHF")
    assert real is not None and real.describe() == "solvent DMF → 2-MeTHF"
    # An unrecognised species is still a real condition, and two of them still differ.
    assert text_change("solvent", "Mystery-A", "Mystery-B") is not None
