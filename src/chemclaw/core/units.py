"""A quantity with a dimension: value, unit, uncertainty, basis — and comparisons that refuse.

**The gap this closes is one column.** `infra/sql/030_measurements.sql` declares
`unit TEXT NOT NULL DEFAULT ''`, `science.calc.calibration.record_observation` takes a `unit`
argument and writes it through unexamined, and the tool that calls it
(`connectors/calc/server/tools.py::report_measurement`) never passed one — so every measured value
this system has ever stored carries an empty unit, and a chemist reporting `0.5` for solubility
was stored identically whether they meant log S or mg/mL.

For a chemist that is survivable, because most of what flows internally is energies in one fixed
unit. For **analytical development it is the foundation**: a specification is a quantity with a
limit and a comparison, an ICH limit is a quantity with a basis, a stability trend is a series of
quantities with an extrapolation. None of that is expressible over a bare float, and an assistant
that silently compares 0.15% area to 1.5 ppm is worse than one that refuses.

## What this is not

Not a general unit library. It is the units this domain actually writes down, with the operations
this system actually performs: convert, compare, and refuse across dimensions. There is no algebra
over derived units, because nothing here multiplies a mass by a length — and a registry that could
would be an abstraction with no caller, which is the thing this tree deletes on sight.

`chemclaw.core.quantities.Quantity` is a different object with a similar name, and the two are
deliberately not merged: that one is *a number a payload returned, under the key the tool gave it*
— a label and a float, used to check that a stated figure is grounded in a returned one. It knows
nothing about dimensions and must not, because it reports what a tool said rather than what is
true. This one is a physical quantity. `ARCHITECTURE.md` records the pair.

## Temperature is why `offset` exists

Every other unit here converts by a factor. Celsius does not, and a factor-only registry would
convert 25 °C to 25 K silently — the class of error that reaches a chemist as a plausible number.
"""

from dataclasses import dataclass, replace
from typing import Literal

#: The base dimensions this domain writes down. Deliberately short: current, luminous intensity and
#: angle have no caller here, and a dimension nothing uses is a row nobody checks.
Dimension = Literal[
    "dimensionless",
    "mass",
    "amount",
    "volume",
    "time",
    "temperature",
    "pressure",
    "energy_per_amount",
    "concentration",
    "molar_mass",
    "length",
    "fraction",
]


@dataclass(frozen=True, slots=True)
class Unit:
    """One unit: what it measures, and how it relates to this dimension's reference unit.

    `factor` and `offset` convert *to* the reference: `reference = value * factor + offset`. The
    reference unit of each dimension is the one with `factor == 1.0` and `offset == 0.0`, and it is
    an implementation detail rather than a claim about SI — `fraction`'s reference is the bare
    fraction, and `energy_per_amount`'s is kJ/mol, because those are what this system stores.
    """

    symbol: str
    dimension: Dimension
    factor: float = 1.0
    offset: float = 0.0


# The registry. One row per spelling a chemist actually writes, including the ones that differ only
# in casing or in a micro sign, because a value refused for a spelling is a value not recorded.
_UNITS: dict[str, Unit] = {}


def _register(unit: Unit, *aliases: str) -> None:
    """Add a unit under its symbol and every spelling that means it."""
    for name in (unit.symbol, *aliases):
        key = name.strip().lower()
        if key in _UNITS:  # pragma: no cover - a programming error, caught at import
            raise ValueError(f"unit spelling {name!r} is already registered")
        _UNITS[key] = unit


# Dimensionless, and the two that are dimensionless but *scaled* — which is the distinction that
# makes "0.15% versus 1500 ppm" answerable rather than a coin toss.
_register(Unit("", "dimensionless"), "none", "unitless", "-")
_register(Unit("fraction", "fraction"), "frac")
_register(Unit("%", "fraction", 0.01), "percent", "pct", "% w/w", "%w/w", "area%", "% area")
_register(Unit("ppm", "fraction", 1e-6))
_register(Unit("ppb", "fraction", 1e-9))

_register(Unit("g", "mass"), "gram", "grams")
_register(Unit("kg", "mass", 1e3), "kilogram")
_register(Unit("mg", "mass", 1e-3), "milligram")
_register(Unit("ug", "mass", 1e-6), "µg", "μg", "microgram", "mcg")
_register(Unit("ng", "mass", 1e-9), "nanogram")

_register(Unit("mol", "amount"), "mole", "moles")
_register(Unit("mmol", "amount", 1e-3), "millimole")
_register(Unit("umol", "amount", 1e-6), "µmol", "μmol", "micromole")

_register(Unit("L", "volume"), "l", "litre", "liter")
_register(Unit("mL", "volume", 1e-3), "ml", "millilitre", "milliliter")
_register(Unit("uL", "volume", 1e-6), "µl", "μl", "ul", "microlitre", "microliter")

_register(Unit("s", "time"), "sec", "second", "seconds")
_register(Unit("min", "time", 60.0), "minute", "minutes")
_register(Unit("h", "time", 3600.0), "hr", "hour", "hours")
_register(Unit("d", "time", 86_400.0), "day", "days")

# The reference is kelvin, and the two offsets below are the reason `offset` exists at all.
_register(Unit("K", "temperature"), "kelvin")
_register(Unit("degC", "temperature", 1.0, 273.15), "°c", "c", "celsius", "degreec")

_register(Unit("Pa", "pressure"), "pascal")
_register(Unit("kPa", "pressure", 1e3))
_register(Unit("bar", "pressure", 1e5))
_register(Unit("mbar", "pressure", 1e2), "millibar")

_register(Unit("kJ/mol", "energy_per_amount"), "kj/mol", "kjmol")
_register(Unit("kcal/mol", "energy_per_amount", 4.184), "kcal/mol", "kcalmol")
_register(Unit("hartree", "energy_per_amount", 2625.4996), "eh", "ha", "au")
_register(Unit("eV", "energy_per_amount", 96.485_332), "ev")

_register(Unit("M", "concentration"), "mol/l", "mol/L", "molar")
_register(Unit("mM", "concentration", 1e-3), "mmol/l", "millimolar")
_register(Unit("uM", "concentration", 1e-6), "µm", "μm", "umol/l", "micromolar")
_register(Unit("mg/mL", "concentration", -1.0), "mg/ml", "g/L", "g/l")

_register(Unit("g/mol", "molar_mass"), "g mol-1", "da", "dalton")

_register(Unit("m", "length"), "metre", "meter")
_register(Unit("cm", "length", 1e-2), "centimetre", "centimeter")
_register(Unit("mm", "length", 1e-3), "millimetre", "millimeter")
_register(Unit("nm", "length", 1e-9), "nanometre", "nanometer")
_register(Unit("A", "length", 1e-10), "angstrom", "å", "Å")


class UnitError(ValueError):
    """A unit was unknown, or two quantities could not be compared.

    A `ValueError`, so it travels the same non-retryable path every other bad-data refusal here
    takes: a wrong unit is not a transient fault and retrying it will produce the same answer.
    """


def parse_unit(symbol: str) -> Unit:
    """Resolve a unit spelling, or refuse naming what is known for its shape.

    **Refuses rather than defaulting to dimensionless.** An unknown unit silently treated as bare
    would put "0.5 furlongs" in the same column as "0.5", and every comparison downstream would
    then be arithmetic on a number whose meaning nobody can recover.
    """
    key = symbol.strip().lower()
    if key in _UNITS:
        return _UNITS[key]
    raise UnitError(
        f"unknown unit {symbol!r}. Known spellings include: "
        f"{', '.join(sorted({unit.symbol or '(dimensionless)' for unit in _UNITS.values()}))}"
    )


@dataclass(frozen=True, slots=True)
class Measurement:
    """A value, its unit, what is known about its spread, and what it is a fraction *of*.

    `uncertainty` is in the same unit as `value` and is `None` when none was reported — deliberately
    not zero, because zero is a claim of exactness and "nobody said" is not.

    `basis` is free text and exists for the one thing a unit cannot carry: 0.15% of *what*. An area
    percent, a weight percent and a molar percent are the same unit and different facts, and a
    system that dropped the distinction would compare them. It is never parsed and never compared —
    it travels so a reader can see it.
    """

    value: float
    unit: Unit
    uncertainty: float | None = None
    basis: str = ""

    @classmethod
    def of(
        cls, value: float, unit: str, *, uncertainty: float | None = None, basis: str = ""
    ) -> "Measurement":
        """Build one from a unit spelling, refusing an unknown unit."""
        return cls(value=value, unit=parse_unit(unit), uncertainty=uncertainty, basis=basis)

    def to(self, symbol: str) -> "Measurement":
        """This quantity in another unit of the same dimension, or refuse.

        The uncertainty is scaled by the same factor and **not** by the offset: a spread of 2 °C is
        a spread of 2 K, and adding 273.15 to it would be the classic temperature-conversion bug in
        the one field nobody re-reads.
        """
        target = parse_unit(symbol)
        if target.dimension != self.unit.dimension:
            raise UnitError(
                f"cannot express {self.unit.symbol or 'a dimensionless value'} "
                f"({self.unit.dimension}) as {target.symbol or 'dimensionless'} "
                f"({target.dimension}) — they measure different things"
            )
        reference = self.value * self.unit.factor + self.unit.offset
        converted = (reference - target.offset) / target.factor
        spread = (
            None
            if self.uncertainty is None
            else abs(self.uncertainty * self.unit.factor / target.factor)
        )
        return replace(self, value=converted, unit=target, uncertainty=spread)

    def compare(self, other: "Measurement") -> int:
        """-1, 0 or 1 against another quantity, or refuse across dimensions.

        The refusal is the point. Python would happily order two floats whose units disagree, and
        a specification check written that way passes a batch that is out of limits.
        """
        if other.unit.dimension != self.unit.dimension:
            raise UnitError(
                f"cannot compare {self.unit.dimension} with {other.unit.dimension}: "
                f"{self} and {other} are not the same kind of quantity"
            )
        converted = other.to(self.unit.symbol)
        if self.value < converted.value:
            return -1
        return 1 if self.value > converted.value else 0

    def __str__(self) -> str:
        """`1.50 ± 0.05 mg` — how a chemist writes it, with the basis when there is one."""
        text = f"{self.value:g}"
        if self.uncertainty is not None:
            text += f" ± {self.uncertainty:g}"
        if self.unit.symbol:
            text += f" {self.unit.symbol}"
        return f"{text} ({self.basis})" if self.basis else text


def same_dimension(first: str, second: str) -> bool:
    """Whether two unit spellings measure the same kind of thing. Raises on an unknown one."""
    return parse_unit(first).dimension == parse_unit(second).dimension
