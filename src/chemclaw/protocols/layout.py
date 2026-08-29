"""Turning a list of arms into a plate: wells, labels and run order.

Deterministic arithmetic with no chemistry in it, kept apart from `checks` for the reason
`science/` is kept apart from `connectors/`: a layout is testable without a design being valid, and
`checks.layout_fits` needs to ask questions about a layout it did not itself produce.

**Positions are row-major and run order is what gets shuffled.** Those are two different
confounders and only one of them is worth trading a chemist's time for. Randomising *positions*
makes a plate that has to be pipetted from a lookup table instead of left to right, and buys
protection against an edge effect that a plate map already makes visible; randomising *run order*
is what stops a drift over the session — a decaying stock solution, a warming room — from reading as
a factor effect, which nothing else can catch. `bo`'s `generate_screening_design(randomize=True)`
shuffles the same thing for the same reason.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

from chemclaw.core.errors import ChemclawError
from chemclaw.protocols.models import PlateLayout, ProtocolArm, Well

#: `plate_format -> (rows, columns)`. Five formats: the SBS densities plus 48, which is the one a
#: bench chemist actually reaches for when 24 is too few. A format not in this map is refused
#: rather than guessed at: a 60-well plate would otherwise be laid out as 6x10 or 10x6 and the two
#: are different plates.
PLATE_SHAPES: dict[int, tuple[int, int]] = {
    24: (4, 6),
    48: (6, 8),
    96: (8, 12),
    384: (16, 24),
    1536: (32, 48),
}


class LayoutError(ChemclawError):
    """A layout that cannot be produced — an unknown plate, or more arms than wells."""


def plate_shape(plate_format: int) -> tuple[int, int]:
    """The (rows, columns) of a plate, refusing a format this system does not know."""
    shape = PLATE_SHAPES.get(plate_format)
    if shape is None:
        known = ", ".join(str(k) for k in sorted(PLATE_SHAPES))
        raise LayoutError(f"unknown plate format {plate_format}; known formats are {known}")
    return shape


def row_label(row: int) -> str:
    """The letter for a 0-based row index: A..Z, then AA..AF for a 1536-well plate."""
    # 1536 is 32 rows, so the second letter is needed and `chr(ord("A") + row)` is wrong past Z —
    # it produces `[`, `\`, `]`, which are legal strings and illegal wells.
    if row < 26:
        return chr(ord("A") + row)
    return chr(ord("A") + row // 26 - 1) + chr(ord("A") + row % 26)


def well_label(row: int, column: int) -> str:
    """The well name for 0-based row/column indices — `A1`, `H12`, `AF48`."""
    return f"{row_label(row)}{column + 1}"


def capacity(plate_format: int) -> int:
    """How many arms a plate of this format holds."""
    rows, columns = plate_shape(plate_format)
    return rows * columns


def place(
    arms: Sequence[ProtocolArm],
    *,
    plate_format: int,
    randomized: bool = False,
    seed: int | None = None,
) -> PlateLayout:
    """Lay the arms out row-major and assign a run order.

    Args:
        arms: The arms, in the order they should occupy wells.
        plate_format: 24, 48, 96, 384 or 1536.
        randomized: Shuffle the *run order* (not the positions). Needs a `seed` so the design can
            be reproduced from its stored document.
        seed: The shuffle seed. Required when `randomized`.

    Raises:
        LayoutError: unknown format, more arms than wells, or a randomised layout with no seed.
    """
    rows, columns = plate_shape(plate_format)
    if not arms:
        raise LayoutError("a layout needs at least one arm")
    if len(arms) > rows * columns:
        raise LayoutError(
            f"{len(arms)} arms do not fit a {plate_format}-well plate "
            f"({rows}x{columns}); reduce the design or use a larger plate"
        )
    if randomized and seed is None:
        # A shuffle nobody can reproduce turns the stored document into a different plate from the
        # one the chemist ran, and the run order is exactly what a drift analysis needs back.
        raise LayoutError("a randomized layout needs a seed so the run order can be reproduced")

    order = list(range(len(arms)))
    if randomized:
        random.Random(seed).shuffle(order)
    # `order[i]` is the arm that runs i-th; invert it so each arm knows its own 1-based position.
    run_order = {position: rank + 1 for rank, position in enumerate(order)}

    wells = [
        Well(
            label=well_label(index // columns, index % columns),
            row=index // columns,
            column=index % columns,
            arm_id=arm.arm_id,
            run_order=run_order[index],
        )
        for index, arm in enumerate(arms)
    ]
    return PlateLayout(
        plate_format=plate_format,
        rows=rows,
        columns=columns,
        wells=wells,
        randomized=randomized,
        seed=seed if randomized else None,
    )


def smallest_plate_for(count: int) -> int | None:
    """The smallest known plate that holds `count` arms, or `None` if none does."""
    return next((fmt for fmt in sorted(PLATE_SHAPES) if capacity(fmt) >= count), None)
