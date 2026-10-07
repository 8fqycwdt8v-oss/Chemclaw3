"""Scaling a protocol's charges to a new basis, and naming everything that did not scale.

The arithmetic is trivial; the deliverable is the list of quantities that are *not* multiplied by
the factor (addition times, filtrations, cooling), because they change the time-temperature
history and are invisible in a scaled protocol. `rescale` returns the scaled charges and a
`Caveat` per refused quantity together.

It computes no new setpoint, dose rate or equipment size (that is `connectors/unitops` and
`connectors/thermalsafety`, from measurements), and it writes nothing: the revised design is
stored through `draft_experiment_protocol` like any other revision.
"""

from dataclasses import dataclass

from chemclaw.core.units import Measurement, UnitError, has_ambiguous_comma, parse_quantity
from chemclaw.protocols.models import ChargeLine, ExperimentDesign, ProtocolStep, RequestField

#: Step kinds whose *duration* does not follow the charge, with the reason each one does not.
#: Keyed by kind because the limit is a property of the operation (cake resistance, heat and mass
#: transfer, jacket heat removal), none of which is linear in the charge.
_DURATION_DOES_NOT_SCALE: dict[str, str] = {
    "addition": (
        "an addition time is set by heat removal and by how much unreacted reagent may accumulate, "
        "not by the charge — at scale the jacket is the constraint and the dose is usually longer. "
        "`heat_removal_capacity` and `semibatch_accumulation_profile` are what decide it, from "
        "measured numbers"
    ),
    "purification": (
        "a filtration or a chromatography is limited by area and by cake or column resistance, so "
        "its duration scales with neither the charge nor the volume. `filtration_time` sizes it "
        "from a measured cake resistance"
    ),
    "workup": (
        "phase separation and washing are limited by settling and by interfacial area, which "
        "change with vessel geometry rather than with the charge"
    ),
    "temperature": (
        "a ramp is limited by the jacket's duty against a surface-to-volume ratio that falls as "
        "the vessel grows, so the same ramp rate is not available. `heat_transfer_time_constant` "
        "is the number"
    ),
    "hold": (
        "a hold that exists to let a reaction finish does scale with nothing, and one that exists "
        "because the next operation is busy scales with that operation"
    ),
}


@dataclass(frozen=True, slots=True)
class Caveat:
    """One quantity the rescale refused to touch, and why.

    `where` names it as the protocol does (`step 4`, `setpoints.time_h`) so a reader can find it.
    """

    where: str
    quantity: str
    reason: str


@dataclass(frozen=True, slots=True)
class Rescaled:
    """A design scaled to a new basis, the factor used, and what was deliberately left alone."""

    design: ExperimentDesign
    factor: float
    basis: str
    caveats: tuple[Caveat, ...]


class RescaleError(ValueError):
    """The rescale cannot be computed, naming which half is missing.

    A `ValueError`, so one `except ValueError` at an entry point catches every unusable input.
    """


def _limiting_line(design: ExperimentDesign) -> ChargeLine:
    """The charge line the factor is computed against.

    The limiting reagent, not the total mass: that is what "we ran it at 1 g" means. The checks keep
    the marked line honest; this trusts them.
    """
    limiting = [line for line in design.base.charge if line.limiting]
    if len(limiting) != 1:
        raise RescaleError(
            f"a rescale needs exactly one limiting charge line to scale against; this protocol has "
            f"{len(limiting)}. `charge_is_consistent` reports the same thing as a warning — fix it "
            "there rather than choosing one here"
        )
    return limiting[0]


def _stated_bases(line: ChargeLine) -> list[Measurement]:
    """Every positive amount the limiting line states — mass, amount and volume, in that order."""
    stated = [
        Measurement.of(value, unit)
        for value, unit in (
            (line.mass_mg, "mg"),
            (line.amount_mmol, "mmol"),
            (line.volume_ml, "mL"),
        )
        if value is not None and value > 0.0
    ]
    if not stated:
        raise RescaleError(
            f"the limiting line {line.component!r} states no mass, amount or volume, so there is "
            "nothing to scale from"
        )
    return stated


def _factor(design: ExperimentDesign, target: str) -> tuple[float, str]:
    """How much bigger the new basis is, and the basis as it will be recorded.

    The basis is whichever of the limiting line's stated amounts the target converts to. A target in
    a dimension the line does not state is refused rather than assuming a density or molar mass.
    """
    wanted = parse_quantity(target)
    if wanted is None and has_ambiguous_comma(target):
        raise RescaleError(
            f"{target!r} has a comma that may be a thousands separator or a decimal mark — "
            "write it without one, for example '1.5 kg' or '1500 kg'"
        )
    if wanted is None:
        raise RescaleError(
            f"{target!r} is not a quantity this can scale to — write it as a number and a unit, "
            "for example '2 kg' or '500 mL'"
        )
    stated = _stated_bases(_limiting_line(design))
    for current in stated:
        try:
            converted = wanted.to(current.unit.symbol)
        except UnitError:
            continue
        if converted.value <= 0.0:
            raise RescaleError(f"{target!r} is not a positive quantity")
        return converted.value / current.value, target.strip()
    units = " or ".join(current.unit.symbol for current in stated)
    raise RescaleError(
        f"the limiting charge is stated in {units} and the target in {wanted.unit.symbol}; "
        "converting between them needs a molar mass or a density this protocol does not carry, "
        f"so restate the target in {units}"
    )


#: The dimensions `checks._plausibility_bands` can size a band from; any other falls back to bench
#: ceilings, so a rescale never records one while a readable alternative exists.
_BAND_DIMENSIONS = frozenset({"mass", "volume"})

#: The units a derived scale is written in, largest first, so it reads "6 kg" and not "6e+06 mg".
_READABLE_UNITS: dict[str, tuple[str, ...]] = {"mass": ("kg", "g", "mg"), "volume": ("L", "mL")}


def _readable(quantity: Measurement) -> str:
    """`quantity` in the largest unit of its dimension that keeps the number at or above one."""
    units = _READABLE_UNITS[quantity.unit.dimension]
    for symbol in units:
        converted = quantity.to(symbol)
        if converted.value >= 1.0:
            return str(converted)
    return str(quantity.to(units[-1]))


def _recorded_scale(design: ExperimentDesign, target: str, factor: float) -> str:
    """The request scale a rescaled design declares: always one the plausibility band can read.

    The target when it is a mass or volume; otherwise the chemist's declared scale moved by the
    factor (keeping their unit); then the limiting line's scaled mass or volume; the target text
    only as a last resort.
    """
    wanted = parse_quantity(target)
    if wanted is not None and wanted.unit.dimension in _BAND_DIMENSIONS:
        return target.strip()
    declared = parse_quantity(design.request.scale.value)
    if declared is not None and declared.unit.dimension in _BAND_DIMENSIONS and declared.value > 0:
        return _readable(Measurement(value=declared.value * factor, unit=declared.unit))
    line = _limiting_line(design)
    for value, unit in ((line.mass_mg, "mg"), (line.volume_ml, "mL")):
        if value is not None and value > 0.0:
            return _readable(Measurement.of(value * factor, unit))
    return target.strip()


def _scaled_line(line: ChargeLine, factor: float) -> ChargeLine:
    """One charge line at the new basis.

    Equivalents are not scaled: a ratio is what survives a change of scale.
    """
    return line.model_copy(
        update={
            field: None if value is None else value * factor
            for field, value in (
                ("amount_mmol", line.amount_mmol),
                ("mass_mg", line.mass_mg),
                ("volume_ml", line.volume_ml),
            )
        }
    )


def _step_caveats(steps: list[ProtocolStep]) -> list[Caveat]:
    """A caveat per step whose duration this refuses to scale."""
    return [
        Caveat(where=f"step {step.index}", quantity=f"{step.duration_h} h", reason=reason)
        for step in steps
        if step.duration_h is not None
        and (reason := _DURATION_DOES_NOT_SCALE.get(step.kind)) is not None
    ]


def rescale(design: ExperimentDesign, *, target: str) -> Rescaled:
    """`design` with every charge at `target`, plus everything that did not move.

    Args:
        design: the protocol to scale. Its limiting charge line is the basis.
        target: the new basis as a quantity a person would write: "2 kg", "500 mL".

    Returns:
        The revised design, the factor, the basis as recorded, and one `Caveat` per quantity this
        refuses to scale. The caveats are part of the answer: reporting charges without them is the
        failure this module exists to prevent.

    Raises:
        RescaleError: no single limiting line, a limiting line with no amount, a target that is not
            a quantity, or a target in a dimension the protocol cannot convert without inventing a
            density or a molar mass.
    """
    factor, basis = _factor(design, target)
    body = design.base
    caveats = _step_caveats(list(body.steps))
    if body.setpoints.time_h is not None:
        caveats.append(
            Caveat(
                where="setpoints.time_h",
                quantity=f"{body.setpoints.time_h} h",
                reason=(
                    "a reaction time is a property of the chemistry and not of the batch, so it is "
                    "carried across unchanged — but it was measured under bench mixing and bench "
                    "heat transfer, and at scale both change"
                ),
            )
        )
    if body.setpoints.concentration_molar is not None:
        caveats.append(
            Caveat(
                where="setpoints.concentration_molar",
                quantity=f"{body.setpoints.concentration_molar} M",
                reason=(
                    "concentration is held, which is what scaling every charge by one factor "
                    "means — check the result still fits the vessel's working volume, which "
                    "nothing here knows"
                ),
            )
        )
    # The request's scale moves with the charges so the plausibility bands judge the new batch, not
    # the bench. `inferred`, not `stated`: the chemist's text never said this value.
    scaled = design.model_copy(
        update={
            "request": design.request.model_copy(
                update={
                    "scale": RequestField(
                        value=_recorded_scale(design, target, factor), basis="inferred"
                    )
                }
            ),
            "base": body.model_copy(
                update={"charge": [_scaled_line(line, factor) for line in body.charge]}
            ),
        }
    )
    return Rescaled(design=scaled, factor=factor, basis=basis, caveats=tuple(caveats))
