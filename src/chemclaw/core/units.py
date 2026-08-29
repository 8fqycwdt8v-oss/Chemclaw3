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
    # **A log scale is its own dimension, and each one is its own.** `log S` and `pKa` are both
    # "dimensionless" in the sense that they carry no units, and treating them that way would make
    # them interconvertible with each other and with `%` — so a chemist reporting a pKa into the
    # solubility ledger would be accepted silently. Nothing converts into a log scale, which is
    # exactly what a dimension of its own expresses.
    "log_solubility",
    "acidity",
    # Mass per volume is **not** the same dimension as molarity here, deliberately. Converting
    # mg/mL to M needs the molar mass of the substance, which is a fact about the sample rather
    # than about the units — and this module has no algebra over derived units precisely so that it
    # cannot invent one. Keeping them apart means `0.5 mg/mL` compared against a limit in `mM`
    # refuses, which is the right answer: the conversion needs an input nobody supplied.
    "mass_concentration",
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


# The registry, and the reason there are two of them.
#
# **Unit symbols are case-sensitive, and folding them is a real hazard rather than a pedantry.**
# `M` is molar and `m` is metre; `mM` is millimolar and `mm` is millimetre. A case-insensitive
# lookup makes each of those pairs one spelling and picks whichever was registered first — so a
# limit stated in `mM` would be read as millimetres, refuse nothing, and compare against a
# concentration as though it were a length. Measured while building this: a folded registry could
# not hold molarity and length at once.
#
# So `_UNITS` is exact and `_FOLDED` is the convenience layer, holding a lowercase spelling **only
# while it is unambiguous**. A fold claimed by two different units maps to `None`, and `parse_unit`
# refuses it by name rather than guessing — which is how `mm` behaves once both are registered.
_UNITS: dict[str, Unit] = {}
_FOLDED: dict[str, Unit | None] = {}


def _register(unit: Unit, *aliases: str) -> None:
    """Add a unit under its exact symbol and every spelling that means it."""
    for name in (unit.symbol, *aliases):
        key = name.strip()
        if key in _UNITS:  # pragma: no cover - a programming error, caught at import
            raise ValueError(
                f"unit spelling {key!r} (for {unit.symbol!r}) is already registered for "
                f"{_UNITS[key].symbol!r}"
            )
        _UNITS[key] = unit
        folded = key.lower()
        # Claimed by a *different* unit already: neither may answer for it.
        existing = _FOLDED.get(folded, unit)
        _FOLDED[folded] = unit if existing is unit else None


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

_register(Unit("L", "volume"), "litre", "liter")
_register(Unit("mL", "volume", 1e-3), "millilitre", "milliliter")
_register(Unit("uL", "volume", 1e-6), "µl", "μl", "microlitre", "microliter")

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

_register(Unit("kJ/mol", "energy_per_amount"), "kjmol", "kj mol-1")
_register(Unit("kcal/mol", "energy_per_amount", 4.184), "kcalmol", "kcal mol-1")
_register(Unit("hartree", "energy_per_amount", 2625.4996), "eh", "ha", "au")
_register(Unit("eV", "energy_per_amount", 96.485_332), "electronvolt")

_register(Unit("M", "concentration"), "mol/L", "molar")
_register(Unit("mM", "concentration", 1e-3), "mmol/l", "millimolar")
_register(Unit("uM", "concentration", 1e-6), "µm", "μm", "umol/l", "micromolar")
_register(Unit("mg/mL", "mass_concentration"), "g/L")
_register(Unit("ug/mL", "mass_concentration", 1e-3), "µg/mL", "μg/mL", "mg/L")

_register(Unit("g/mol", "molar_mass"), "g mol-1", "da", "dalton")

# The two calibrated properties' own scales, spelled exactly as `_CALIBRATED` spells them in
# `connectors/calc/server/tools.py`, because the ledger's unit column and this registry have to
# agree on the string or the check is a no-op.
_register(Unit("log S", "log_solubility"), "logs", "log10(mol/l)")
_register(Unit("pKa", "acidity"))

_register(Unit("m", "length"), "metre", "meter")  # `mm`/`cm`/`nm` follow below
_register(Unit("cm", "length", 1e-2), "centimetre", "centimeter")
_register(Unit("mm", "length", 1e-3), "millimetre", "millimeter")
_register(Unit("nm", "length", 1e-9), "nanometre", "nanometer")
_register(Unit("angstrom", "length", 1e-10), "å", "ang")


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
    key = symbol.strip()
    if key in _UNITS:
        return _UNITS[key]
    folded = _FOLDED.get(key.lower(), "missing")
    if folded is None:
        raise UnitError(
            f"unit {symbol!r} is ambiguous once case is ignored — this domain distinguishes M from "
            "m and mM from mm, so write the symbol exactly"
        )
    if isinstance(folded, Unit):
        return folded
    raise UnitError(
        f"unknown unit {symbol!r}. Known symbols: "
        f"{', '.join(sorted(unit.symbol or '(dimensionless)' for unit in set(_UNITS.values())))}"
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


def reconcile(value: float, reported: str, expected: str) -> float:
    """`value`, reported in `reported`, expressed in `expected` — or refuse.

    The one call a ledger makes. `reported` empty means the caller stated no unit, which is
    accepted as "the ledger's own unit" rather than refused: every measurement stored before this
    existed carries an empty unit, and refusing the unstated case would make the common path fail
    while the *wrong* path (a number in the wrong unit, silently stored) is the one this exists to
    catch.

    Raises:
        UnitError: When `reported` is unknown, or measures something `expected` does not. A pKa
            reported into the solubility ledger, or a `mg/mL` where the column holds log S, is
            refused here rather than becoming a residual nobody can explain.
    """
    if not reported.strip():
        return value
    return Measurement.of(value, reported).to(expected).value
