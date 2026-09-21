"""Scaling a protocol's charges to a new basis, and naming everything that did not scale.

**The arithmetic is the easy half and is not why this module exists.** Multiplying every charge by
a factor is four lines; what a chemist taking a 1 g procedure to 2 kg actually needs is the list of
quantities that are *not* multiplied by that factor, because those are where the batch goes wrong
and they are invisible in the scaled protocol. An addition made over ten minutes on the bench is
not made over ten minutes in a 250 L reactor; a filtration that took twenty minutes takes a shift;
cooling that was instant is now a time constant. Each of those changes the time-temperature history
the material sees, which is the thing the original procedure was actually evidence about.

So `rescale` returns both halves and the caller cannot take one without the other: the scaled
charges, and a `Caveat` per quantity this module refuses to scale. The refusals are the deliverable.

**What this module does not do.** It computes no new setpoint, proposes no dose rate and sizes no
equipment — `connectors/unitops` and `connectors/thermalsafety` do that from measurements, and
inventing a dose time here would be exactly the fabricated-input failure
`D-2026-09-20-a-ranking-is-evidence-a-critic-is-not-a-gate` refuses one layer over. It also writes
nothing: it returns a revised `ExperimentDesign`, and storing it is `draft_experiment_protocol`'s
job under the ordinary `parent_revision` check, so a rescale is a revision a human can diff and
reject like any other.
"""

from dataclasses import dataclass

from chemclaw.core.units import Measurement, UnitError, parse_quantity
from chemclaw.protocols.models import ChargeLine, ExperimentDesign, ProtocolStep

#: Step kinds whose *duration* does not follow the charge, with the reason each one does not.
#:
#: Keyed by kind rather than by a per-protocol judgement because the reason is a property of the
#: operation: a filtration is limited by cake resistance and filter area, a drying by heat and
#: mass transfer through a deeper bed, an addition by the jacket's ability to remove the heat it
#: releases. None of those is linear in the charge, and two of them are the reason a scaled batch
#: has a different impurity profile than the procedure it was scaled from.
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

    `where` names it the way the protocol does (`step 4`, `setpoints.time_h`) so a reader can find
    it, rather than describing it.
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

    A `ValueError` for the reason `ConnectorError` is: these are all "this input is not usable"
    failures that one `except ValueError` at an entry point catches.
    """


def _limiting_line(design: ExperimentDesign) -> ChargeLine:
    """The charge line the factor is computed against.

    The limiting reagent and not the total mass, because that is what a chemist means by "we ran it
    at 1 g" and what `stoichiometry_table` already scales against. `charge_is_consistent` and
    `limiting_is_limiting` are what keep the marked line honest; this module trusts them rather
    than re-deriving the answer, which is the DRY rule and also keeps one definition of limiting.
    """
    limiting = [line for line in design.base.charge if line.limiting]
    if len(limiting) != 1:
        raise RescaleError(
            f"a rescale needs exactly one limiting charge line to scale against; this protocol has "
            f"{len(limiting)}. `charge_is_consistent` reports the same thing as a warning — fix it "
            "there rather than choosing one here"
        )
    return limiting[0]


def _current_basis(line: ChargeLine) -> Measurement:
    """The limiting line's amount, as a quantity a target can be divided by."""
    if line.mass_mg is not None and line.mass_mg > 0.0:
        return Measurement.of(line.mass_mg, "mg")
    if line.amount_mmol is not None and line.amount_mmol > 0.0:
        return Measurement.of(line.amount_mmol, "mmol")
    if line.volume_ml is not None and line.volume_ml > 0.0:
        return Measurement.of(line.volume_ml, "mL")
    raise RescaleError(
        f"the limiting line {line.component!r} states no mass, amount or volume, so there is "
        "nothing to scale from"
    )


def _factor(design: ExperimentDesign, target: str) -> tuple[float, str]:
    """How much bigger the new basis is, and the basis as it will be recorded.

    Refuses a target in a dimension the limiting line does not state rather than assuming a
    density or a molar mass. Assuming either is how a scaled protocol acquires a number nobody
    measured, and the caller can restate the target in the dimension the protocol already uses.
    """
    wanted = parse_quantity(target)
    if wanted is None:
        raise RescaleError(
            f"{target!r} is not a quantity this can scale to — write it as a number and a unit, "
            "for example '2 kg' or '500 mL'"
        )
    current = _current_basis(_limiting_line(design))
    try:
        converted = wanted.to(current.unit.symbol)
    except UnitError as error:
        raise RescaleError(
            f"the limiting charge is stated in {current.unit.symbol} and the target in "
            f"{wanted.unit.symbol}; converting between them needs a molar mass or a density this "
            f"protocol does not carry, so restate the target in {current.unit.symbol}. ({error})"
        ) from error
    if converted.value <= 0.0:
        raise RescaleError(f"{target!r} is not a positive quantity")
    return converted.value / current.value, target.strip()


def _scaled_line(line: ChargeLine, factor: float) -> ChargeLine:
    """One charge line at the new basis.

    Equivalents are **not** scaled and that is the whole point of the field: a ratio is what
    survives a change of scale, and multiplying it would silently change the chemistry rather than
    the batch size.
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
        target: the new basis as a quantity a person would write — "2 kg", "500 mL".

    Returns:
        The revised design, the factor, the basis as recorded, and one `Caveat` per quantity this
        refuses to scale. **The caveats are not advisory decoration**: a caller that reports the
        charges without them has produced the document that makes a scaled batch fail, which is the
        failure this module was written for.

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
    scaled = design.model_copy(
        update={
            "base": body.model_copy(
                update={"charge": [_scaled_line(line, factor) for line in body.charge]}
            )
        }
    )
    return Rescaled(design=scaled, factor=factor, basis=basis, caveats=tuple(caveats))
