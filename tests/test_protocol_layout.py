"""Plate arithmetic — the part of a design that is wrong silently when it is wrong.

Two things here are traps rather than formalities. `chr(ord("A") + row)` is the obvious row label
and it produces bracket characters past Z, which are legal strings and illegal wells — a 1536-well
plate has 32 rows, so this is reachable rather than theoretical. And `place` randomises the **run
order** while leaving the positions row-major; a reader who assumed the opposite would pipette from
a lookup table that does not exist, so the contract is asserted in both halves.
"""

import pytest

from chemclaw.core.errors import ChemclawError
from chemclaw.protocols.layout import (
    PLATE_SHAPES,
    LayoutError,
    capacity,
    place,
    plate_shape,
    row_label,
    smallest_plate_for,
    well_label,
)
from chemclaw.protocols.models import ProtocolArm


def _arms(count: int) -> list[ProtocolArm]:
    """`count` arms with ids a well label can never be confused with."""
    return [ProtocolArm(arm_id=f"arm-{index:04d}") for index in range(count)]


def test_row_label_covers_the_first_twenty_six_rows_as_single_letters() -> None:
    assert row_label(0) == "A"
    assert row_label(7) == "H"
    assert row_label(25) == "Z"


def test_row_label_past_z_is_two_letters_and_not_a_bracket() -> None:
    """The trap: `chr(ord("A") + 26)` is `[`, which is a legal string and an illegal well."""
    assert row_label(26) == "AA"
    assert row_label(27) == "AB"
    assert row_label(31) == "AF"


def test_a_1536_plates_thirty_two_rows_are_all_letters() -> None:
    """The reachable case, asserted over the whole plate rather than at the boundary only."""
    rows, _ = PLATE_SHAPES[1536]
    assert rows == 32
    labels = [row_label(row) for row in range(rows)]
    assert all(label.isalpha() and label.isupper() for label in labels)
    assert len(set(labels)) == rows
    assert labels[-1] == "AF"


def test_well_label_joins_the_row_letter_to_a_one_based_column() -> None:
    assert well_label(0, 0) == "A1"
    assert well_label(7, 11) == "H12"
    assert well_label(31, 47) == "AF48"


def test_every_plate_shape_multiplies_out_to_its_own_key() -> None:
    """The map is the only statement of a plate's geometry, so it has to agree with itself."""
    for plate_format, (rows, columns) in PLATE_SHAPES.items():
        assert rows * columns == plate_format
        assert capacity(plate_format) == plate_format
        assert plate_shape(plate_format) == (rows, columns)


def test_plate_shape_refuses_a_format_this_system_does_not_know() -> None:
    """A 60-well plate would be guessed as 6x10 or 10x6, and the two are different plates."""
    with pytest.raises(LayoutError, match="unknown plate format 60"):
        plate_shape(60)


def test_a_layout_error_is_a_chemclaw_error() -> None:
    """It reaches a tool caller through the same family every other refusal here does."""
    assert issubclass(LayoutError, ChemclawError)


def test_place_lays_arms_out_row_major_in_arm_order() -> None:
    layout = place(_arms(8), plate_format=24)
    assert [well.label for well in layout.wells] == ["A1", "A2", "A3", "A4", "A5", "A6", "B1", "B2"]
    assert [(well.row, well.column) for well in layout.wells[:7]] == [
        (0, 0),
        (0, 1),
        (0, 2),
        (0, 3),
        (0, 4),
        (0, 5),
        (1, 0),
    ]
    assert [well.arm_id for well in layout.wells] == [arm.arm_id for arm in _arms(8)]
    assert [well.run_order for well in layout.wells] == list(range(1, 9))
    assert (layout.rows, layout.columns) == PLATE_SHAPES[24]
    assert layout.plate_format == 24
    assert layout.randomized is False and layout.seed is None


def test_place_refuses_an_unknown_format() -> None:
    with pytest.raises(LayoutError, match="unknown plate format 60"):
        place(_arms(2), plate_format=60)


def test_place_refuses_more_arms_than_the_plate_holds() -> None:
    with pytest.raises(LayoutError, match="25 arms do not fit a 24-well plate"):
        place(_arms(25), plate_format=24)


def test_place_fills_a_plate_exactly_to_capacity() -> None:
    """The boundary the refusal above sits one arm beyond."""
    layout = place(_arms(24), plate_format=24)
    assert len(layout.wells) == 24
    assert layout.wells[-1].label == "D6"


def test_place_refuses_a_layout_with_no_arms() -> None:
    with pytest.raises(LayoutError, match="at least one arm"):
        place([], plate_format=24)


def test_place_refuses_a_randomized_layout_with_no_seed() -> None:
    """A shuffle nobody can reproduce makes the stored document a different plate from the run."""
    with pytest.raises(LayoutError, match="needs a seed"):
        place(_arms(4), plate_format=24, randomized=True)


def test_a_randomized_layout_with_a_fixed_seed_is_reproducible() -> None:
    first = place(_arms(12), plate_format=24, randomized=True, seed=7)
    second = place(_arms(12), plate_format=24, randomized=True, seed=7)
    assert [well.run_order for well in first.wells] == [well.run_order for well in second.wells]
    assert first.seed == 7 and first.randomized is True


def test_a_different_seed_gives_a_different_run_order() -> None:
    """Otherwise the seed would be recorded and ignored, which reads identically from outside."""
    orders = {
        seed: tuple(
            well.run_order
            for well in place(_arms(12), plate_format=24, randomized=True, seed=seed).wells
        )
        for seed in (1, 2, 3, 4)
    }
    assert len(set(orders.values())) > 1


def test_a_randomized_run_order_is_a_permutation_of_one_to_n() -> None:
    """Every arm runs exactly once, whatever the shuffle did."""
    layout = place(_arms(20), plate_format=24, randomized=True, seed=11)
    assert sorted(well.run_order for well in layout.wells) == list(range(1, 21))


def test_randomizing_shuffles_the_run_order_and_never_the_positions() -> None:
    """The documented contract, and the half a reader would otherwise assume the other way round.

    Randomising positions buys protection against an edge effect a plate map already makes visible,
    at the cost of pipetting from a lookup table; randomising the run order is what stops a drift
    over the session from reading as a factor effect.
    """
    arms = _arms(12)
    ordered = place(arms, plate_format=24)
    shuffled = place(arms, plate_format=24, randomized=True, seed=3)

    assert [well.label for well in shuffled.wells] == [well.label for well in ordered.wells]
    assert [well.arm_id for well in shuffled.wells] == [well.arm_id for well in ordered.wells]
    assert [(well.row, well.column) for well in shuffled.wells] == [
        (well.row, well.column) for well in ordered.wells
    ]
    assert [well.run_order for well in shuffled.wells] != [well.run_order for well in ordered.wells]


def test_place_records_no_seed_when_it_did_not_shuffle() -> None:
    """A seed on an unshuffled plate would claim a randomisation that never happened."""
    layout = place(_arms(4), plate_format=24, randomized=False, seed=7)
    assert layout.randomized is False and layout.seed is None


def test_smallest_plate_for_picks_the_smallest_that_fits() -> None:
    assert smallest_plate_for(1) == 24
    assert smallest_plate_for(24) == 24
    assert smallest_plate_for(25) == 48
    assert smallest_plate_for(49) == 96
    assert smallest_plate_for(1536) == 1536


def test_smallest_plate_for_answers_none_when_nothing_holds_the_design() -> None:
    """`None` rather than the largest plate, so a caller cannot suggest one that does not fit."""
    assert smallest_plate_for(1537) is None
