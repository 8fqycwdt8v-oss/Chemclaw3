"""A quantity with a dimension: value, unit, uncertainty, basis — and comparisons that refuse.

A specification is a quantity with a limit and a comparison, an ICH limit a quantity with a basis, a
stability trend a series of quantities; none of that is expressible over a bare float, and silently
comparing 0.15% area to 1.5 ppm is worse than refusing.

Not a general unit library: the units this domain writes down, with convert, compare and refuse
across dimensions, and no derived-unit algebra. Distinct from `chemclaw.core.quantities.Quantity`,
which is a number a payload returned under its key, with no notion of dimension. Celsius is why
`offset` exists: a factor-only registry would turn 25 °C into 25 K.

The prefixed rungs are generated as a cross product of `_LADDER` with the base units, so molar (`M`)
and metre (`m`), which differ only by case, always get the same rungs and a spelling can never fold
silently onto the other ladder. `pint` supplies the prefix factors and computes every conversion
factor, from a restricted registry (`pint.UnitRegistry(None)` told only `_PREFIX_DEFINITIONS` and
`_DEFS`), so `furlong`, `m/g` and `m**2` are refused. Generated spellings are checked with
`UnitRegistry.get_name`, which resolves only prefixes and aliases; `parse_unit` is a lookup over the
generated spellings, narrower than everything the registry could spell.

Not delegated to pint: uncertainty (its `Measurement` needs `uncertainties`; a spread converts by
factor, never offset), `basis` (a property of the sample), case folding (`_FOLDED`), and the
per-call arithmetic, so refusals are `UnitError` naming the chemist's spelling rather than pint's
`DimensionalityError`.
"""

import re
from dataclasses import dataclass, replace
from typing import Literal, get_args

import pint
from scipy import constants as _constants

# The base dimensions this domain writes down, each a `pint` base dimension declared `[name]` in
# `_DEFS` and checked against this tuple at import. Nothing is derived, which keeps `mg/mL` and `M`
# incomparable and the registry free of derived-unit algebra.
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
    # Each log scale is its own dimension: otherwise `log S`, `pKa` and `%` would be
    # interconvertible and a pKa reported into the solubility ledger would be accepted.
    "log_solubility",
    "acidity",
    # Mass per volume is not molarity: converting needs the substance's molar mass, a fact about the
    # sample, so `0.5 mg/mL` against a limit in `mM` refuses.
    "mass_concentration",
]


@dataclass(frozen=True, slots=True)
class Unit:
    """One unit: what it measures, and how it relates to this dimension's reference unit.

    `reference = value * factor + offset`. The reference is the unit declared `[dimension]` in
    `_DEFS`, chosen for what this system stores (the bare fraction, kJ/mol), not a claim about SI.
    Both numbers are read from the restricted `pint` registry at import, never typed.
    """

    symbol: str
    dimension: Dimension
    factor: float = 1.0
    offset: float = 0.0


class UnitError(ValueError):
    """A unit was unknown, or two quantities could not be compared.

    A `ValueError`, so it takes the non-retryable bad-data path. Every refusal here is this type;
    pint's `UndefinedUnitError` and `DimensionalityError` are translated where they arise, because a
    caller's `except UnitError` decides whether a chemist sees a message or a traceback.
    """


# The constants below are read from `scipy.constants` (CODATA) rather than transcribed, so there is
# one definition. They are presentation conversions applied after cache lookups (the cache stores
# hartrees, and these are not in its key), so a CODATA update changes no stored row.
# `tests/test_units.py` pins each against its literal with `==`, so a scipy upgrade carrying new
# CODATA values is adopted deliberately.

# The thermochemical calorie in joules, exact by definition; read from `scipy.constants` so all
# three constants share one source.
JOULE_PER_CALORIE = _constants.calorie

# One hartree in kcal/mol, from CODATA's `E_h`, `N_A` and the calorie above. The one definition:
# `science/calc/thermo.py` and `publish/properties.py` import it.
HARTREE_TO_KCAL = _constants.value("Hartree energy") * _constants.N_A / 1000.0 / JOULE_PER_CALORIE

# One electronvolt in kJ/mol: `N_A·e`, both exact under SI-2019.
ELECTRONVOLT_TO_KJ = _constants.e * _constants.N_A / 1000.0


@dataclass(frozen=True, slots=True)
class _Def:
    """One unprefixed unit: what to tell `pint`, what to call it here, and which rungs it has.

    `definition` is a pint definition line and the only place a conversion factor appears. `symbol`
    is what a chemist writes and `__str__` prints (pint names cannot contain spaces, so `log S` is
    `logs` to pint). `spellings` are further spellings pint cannot hold, each containing a space.
    """

    definition: str
    symbol: str
    spellings: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()


#: The prefixes, as `pint` definition lines. The factor lives here and nowhere else.
_PREFIX_DEFINITIONS: tuple[str, ...] = (
    "centi- = 1e-2 = c-",
    "milli- = 1e-3 = m-",
    # Both micro signs, because the micro sign (U+00B5) and the Greek mu (U+03BC) are different
    # code points and a chemist may type either.
    "micro- = 1e-6 = u- = µ- = μ-",
    "nano- = 1e-9 = n-",
    "pico- = 1e-12 = p-",
    "kilo- = 1e3 = k-",
)

# The one tuple both the concentration and length ladders are built from, so a rung cannot exist on
# one ladder and not the other; `M` and `m` differ only by case.
_LADDER: tuple[str, ...] = ("centi", "milli", "micro", "nano", "pico")

#: Every unit this domain writes down. One row per *unprefixed* unit: the prefixed rungs are
#: generated, which is why `nM`, `pM` and `µm` are not rows here and cannot be forgotten.
_DEFS: tuple[_Def, ...] = (
    # Dimensionless and scaled-dimensionless units, so "0.15% versus 1500 ppm" is answerable.
    # `fraction` is its own base dimension, not pint's `dimensionless`, so a bare number and a
    # percentage stay incomparable (`reconcile(0.15, "%", "")` refuses).
    _Def("fraction = [fraction] = frac", "fraction"),
    # **A percent is one unit and several facts**, and `_BASIS_SPELLINGS` below is what keeps the
    # facts apart. The spellings with a space in them are first-party for the reason `_Def` gives.
    _Def(
        "percent = 0.01 fraction = % = pct = %w/w = w/w% = area% = %area = mol%",
        "%",
        ("% w/w", "% area", "% mol"),
    ),
    _Def("ppm = 1e-6 fraction", "ppm"),
    _Def("ppb = 1e-9 fraction", "ppb"),
    _Def("gram = [mass] = g = grams", "g", prefixes=("kilo", "milli", "micro", "nano")),
    _Def("mole = [amount] = mol = moles", "mol", prefixes=("milli", "micro")),
    _Def("liter = [volume] = L = litre", "L", prefixes=("milli", "micro")),
    _Def("second = [time] = s = sec = seconds", "s"),
    _Def("minute = 60 second = min = minutes", "min"),
    _Def("hour = 3600 second = h = hr = hours", "h"),
    _Def("day = 86400 second = d = days", "d"),
    # The reference is kelvin, and the offset below is the reason `offset` exists at all.
    _Def("kelvin = [temperature] = K", "K"),
    _Def("degC = kelvin; offset: 273.15 = degreec = celsius = °c = c", "degC"),
    _Def("pascal = [pressure] = Pa", "Pa", prefixes=("kilo",)),
    _Def("bar = 1e5 pascal", "bar", prefixes=("milli",)),
    # kJ/mol is the reference; hartree and electronvolt derive from the constants above.
    # `f"{...!r}"` round-trips exactly, so the registry factor equals the product bit for bit
    # (`tests/test_units.py` asserts with `==`).
    _Def("kJ_per_mol = [energy_per_amount] = kJ/mol = kjmol", "kJ/mol", ("kj mol-1",)),
    _Def(
        f"kcal_per_mol = {JOULE_PER_CALORIE!r} kJ_per_mol = kcal/mol = kcalmol",
        "kcal/mol",
        ("kcal mol-1",),
    ),
    _Def(f"hartree = {HARTREE_TO_KCAL * JOULE_PER_CALORIE!r} kJ_per_mol = eh = ha = au", "hartree"),
    _Def(f"electronvolt = {ELECTRONVOLT_TO_KJ!r} kJ_per_mol = eV", "eV"),
    # The two ladders that differ only by case, both prefixed from `_LADDER`: read these two
    # `prefixes=_LADDER` arguments as a pair.
    _Def("molar = [concentration] = M = mol/L", "M", prefixes=_LADDER),
    _Def("meter = [length] = m = metre", "m", prefixes=_LADDER),
    _Def("angstrom = 1e-10 meter = Å = ang", "angstrom"),
    # `% w/v` is grams per 100 mL by definition, so it is a mass concentration (1 % w/v = 10 mg/mL),
    # not a fraction; as a fraction it would silently assume a density of 1 g/mL.
    _Def("mg_per_mL = [mass_concentration] = mg/mL = g/L", "mg/mL"),
    _Def("ug_per_mL = 1e-3 mg_per_mL = ug/mL = µg/mL = μg/mL = mg/L", "ug/mL"),
    _Def("percent_wv = 10 mg_per_mL = %w/v = w/v%", "% w/v", ("% w/v",)),
    _Def("g_per_mol = [molar_mass] = g/mol = da = dalton", "g/mol", ("g mol-1",)),
    # The calibrated properties' own scales, spelled exactly as `_CALIBRATED` spells them in
    # `connectors/calc/server/tools.py`; the ledger's unit column and this registry must agree.
    _Def("log_solubility = [log_solubility] = logs = log10(mol/l)", "log S", ("log S",)),
    _Def("pKa = [acidity]", "pKa"),
)

# Spellings attached to a generated rung (a pint alias would attach to the base unit and every
# prefix), such as "micron" for the micrometre. The build refuses a key no rung generated.
_RUNG_SPELLINGS: dict[str, tuple[str, ...]] = {
    "um": ("micron",),
    "ug": ("mcg",),
}

# Spellings that state their own basis: an HPLC area percent, a weight percent, a molar percent are
# one unit and different facts. Mapping spelling to basis lets the distinction survive parsing, so
# an `area%` cannot compare equal to a `% w/w`. Keyed and looked up lowercase, since `parse_unit` is
# case-insensitive (`Area%` is the usual printout spelling).
_BASIS_SPELLINGS: dict[str, str] = {
    "% w/w": "w/w",
    "%w/w": "w/w",
    "w/w%": "w/w",
    "area%": "area",
    "% area": "area",
    "%area": "area",
    "mol%": "mol",
    "% mol": "mol",
}


# The registry, in two forms. Unit symbols are case-sensitive (`mM` millimolar vs `mm` millimetre),
# so `_UNITS` is exact, and `_FOLDED` is the case-insensitive convenience layer, holding a lowercase
# spelling only while unambiguous; a fold claimed by two units maps to `None` and `parse_unit`
# refuses it by name.
_UNITS: dict[str, Unit] = {}
_FOLDED: dict[str, Unit | None] = {}

# `None`: start empty rather than loading pint's own definitions, then add only
# `_PREFIX_DEFINITIONS` and `_DEFS`.
_UREG: pint.UnitRegistry[float] = pint.UnitRegistry(None)


def _forms(definition: str) -> tuple[str, tuple[str, ...]]:
    """`('micro', ('u', 'µ', 'μ'))` — a definition line's name and its symbol and aliases.

    Parsed from the line handed to pint so the two cannot disagree. The value field is skipped; a
    prefix's trailing `-` is dropped.
    """
    parts = [part.strip().rstrip("-") for part in definition.split("=")]
    return parts[0], tuple(parts[2:])


def _is_word(form: str) -> bool:
    """Whether a spelling takes a prefix's *name* (`millimolar`) or its *symbol* (`mM`).

    A spelled-out word pairs with the spelled-out prefix; `mol/L` pairs with `m` to make `mmol/L`. A
    unit's own symbol always takes the symbol form too, since `mol` would otherwise read as a word.
    """
    return form.isalpha() and form.islower() and len(form) > 2


def _dimension_of(name: str) -> Dimension:
    """The `Dimension` pint derived for a unit, refusing anything this module has not declared.

    Read off the registry so a misspelled dimension in a definition line cannot mint a new one.
    """
    derived = str(_UREG.get_dimensionality(_UREG.Unit(name))).strip("[]")
    if derived not in get_args(Dimension):
        raise UnitError(f"unit {name!r} has dimension {derived!r}, which this domain does not use")
    return derived  # type: ignore[return-value]


def _add(unit: Unit, spellings: tuple[str, ...]) -> None:
    """Register one unit under every spelling that means it, refusing a spelling claimed twice."""
    for name in spellings:
        key = name.strip()
        if key in _UNITS and _UNITS[key] is not unit:  # pragma: no cover - caught at import
            raise UnitError(
                f"unit spelling {key!r} (for {unit.symbol!r}) is already registered for "
                f"{_UNITS[key].symbol!r}"
            )
        _UNITS[key] = unit
        folded = key.lower()
        # Claimed by a *different* unit already: neither may answer for it.
        existing = _FOLDED.get(folded, unit)
        _FOLDED[folded] = unit if existing is unit else None


def _build() -> None:
    """Load the restricted registry and generate every rung of every ladder from it.

    Runs once at import; dimension, factor, offset and spellings all come from `_PREFIX_DEFINITIONS`
    and `_DEFS`.
    """
    for line in _PREFIX_DEFINITIONS:
        _UREG.define(line)
    for definition in _DEFS:
        _UREG.define(definition.definition)

    prefixes = dict(_forms(line) for line in _PREFIX_DEFINITIONS)
    # The unit each dimension is measured against: the one declared `[dimension]`.
    references = {
        _dimension_of(_forms(d.definition)[0]): _forms(d.definition)[0]
        for d in _DEFS
        if d.definition.split("=")[1].strip().startswith("[")
    }

    # A copy, emptied as rungs claim entries, so leftovers name a rung no longer generated.
    unplaced = dict(_RUNG_SPELLINGS)

    # A bare number is not a unit pint can be told about, and it must not become pint's
    # `dimensionless` — that is one dimension with `fraction`, and `0.15` would then be a `0.15 %`.
    _add(Unit("", "dimensionless"), ("", "none", "unitless", "-"))

    for definition in _DEFS:
        name, pint_forms = _forms(definition.definition)
        dimension = _dimension_of(name)
        reference = references[dimension]
        symbol_forms = (definition.symbol, *(f for f in pint_forms if not _is_word(f)))
        # A pint name is a spelling only when a chemist would write it; `kJ_per_mol` and `mg_per_mL`
        # exist only because pint names cannot hold a slash or space.
        word_forms = (*([name] if "_" not in name else []), *(f for f in pint_forms if _is_word(f)))
        rungs: tuple[tuple[str, str, tuple[str, ...]], ...] = (
            (name, definition.symbol, (*symbol_forms, *word_forms, *definition.spellings)),
            *(
                (
                    prefix + name,
                    prefixes[prefix][0] + definition.symbol,
                    tuple(p + f for p in prefixes[prefix] for f in symbol_forms)
                    + tuple(prefix + f for f in word_forms),
                )
                for prefix in definition.prefixes
            ),
        )
        for pint_name, symbol, spellings in rungs:
            offset = float(_UREG.Quantity(0.0, pint_name).to(reference).magnitude)
            factor = float(_UREG.Quantity(1.0, pint_name).to(reference).magnitude) - offset
            unit = Unit(symbol, dimension, factor, offset)
            _add(unit, (*spellings, *unplaced.pop(symbol, ())))
            # Every spelling pint can hold must reach this unit through pint, or the generator and
            # registry disagree.
            for spelling in spellings:
                if " " in spelling:
                    continue  # a pint name may not contain one; `_Def.spellings` says so
                if _UREG.get_name(spelling) != pint_name:
                    raise UnitError(  # pragma: no cover - caught at import
                        f"{spelling!r} means {pint_name!r} here and "
                        f"{_UREG.get_name(spelling)!r} to the registry"
                    )

    if unplaced:  # pragma: no cover - caught at import
        raise UnitError(f"no rung was generated for {sorted(unplaced)}")


_build()


def parse_unit(symbol: str) -> Unit:
    """Resolve a unit spelling, or refuse naming what is known for its shape.

    Refuses rather than defaulting to dimensionless, which would make downstream comparisons
    meaningless. A lookup rather than a pint call, since the registry would accept any prefix on any
    unit (`kpKa`, `centimole`).
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

    `uncertainty` is in `value`'s unit and `None` when unreported (zero would claim exactness).
    `basis` carries what a unit cannot (0.15% of what): a spelling that states it (`area%`, `% w/w`,
    `mol%`) fills it at parse time, and `compare` refuses two quantities whose stated bases
    disagree. An unstated basis never blocks a comparison.
    """

    value: float
    unit: Unit
    uncertainty: float | None = None
    basis: str = ""

    @classmethod
    def of(
        cls, value: float, unit: str, *, uncertainty: float | None = None, basis: str = ""
    ) -> "Measurement":
        """Build one from a unit spelling, refusing an unknown unit.

        A spelling that states its basis fills `basis` (see `_BASIS_SPELLINGS`); an explicit
        `basis=` always wins.
        """
        return cls(
            value=value,
            unit=parse_unit(unit),
            uncertainty=uncertainty,
            basis=basis or _BASIS_SPELLINGS.get(unit.strip().lower(), ""),
        )

    def to(self, symbol: str) -> "Measurement":
        """This quantity in another unit of the same dimension, or refuse.

        The uncertainty is scaled by the factor and never shifted by the offset: a spread of 2 °C is
        a spread of 2 K.
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

        Ordering floats whose units disagree would pass an out-of-limit batch, hence the refusal.
        """
        if other.unit.dimension != self.unit.dimension:
            raise UnitError(
                f"cannot compare {self.unit.dimension} with {other.unit.dimension}: "
                f"{self} and {other} are not the same kind of quantity"
            )
        # Two stated bases that disagree are not comparable (same unit, different facts). Only
        # refused when both are stated, so ordinary percentages stay comparable.
        if self.basis and other.basis and self.basis != other.basis:
            raise UnitError(
                f"cannot compare {self.basis} with {other.basis}: {self} and {other} are the same "
                "unit measured against different things"
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


def reconcile(value: float, reported: str, expected: str) -> float:
    """`value`, reported in `reported`, expressed in `expected` — or refuse.

    The one call a ledger makes. An empty `reported` is accepted as the ledger's own unit:
    production callers refuse an unstated unit for calibrated properties first, but older stored
    rows carry empty units. Stated bases that disagree are refused here as in `compare`.

    Raises:
        UnitError: When `reported` is unknown, measures something `expected` does not, or states
            a basis that disagrees with `expected`'s (a pKa into the solubility ledger, `mg/mL`
            where the column holds log S, `area%` into a `% w/w` column).
    """
    if not reported.strip():
        return value
    measured = Measurement.of(value, reported)
    target = Measurement.of(0.0, expected)
    if measured.basis and target.basis and measured.basis != target.basis:
        raise UnitError(
            f"cannot record a {measured.basis} value in a {target.basis} column: {reported!r} and "
            f"{expected!r} are the same unit measured against different things"
        )
    return measured.to(expected).value


# A leading number and a trailing unit, with optional space, sign and exponent. Anchored at both
# ends: it reads a field whose whole answer is a quantity ("20 kg"), not a number inside a sentence
# (that is `quantities.labelled_values`'s job).
_QUANTITY = re.compile(r"^\s*([+-]?\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?)\s*([^\s\d].*?)\s*$")

# A comma that could be a thousands separator: one to three digits not starting with 0, a comma,
# exactly three digits. "1,500" is 1500 or 1.5 depending on the reader, so it is refused rather than
# guessed (a factor of 1000 in a rescaled protocol). "0,500" reads as a decimal. Matched on the
# mantissa, so "1,500e3" is refused too.
_AMBIGUOUS_COMMA = re.compile(r"[+-]?[1-9]\d{0,2},\d{3}")


def has_ambiguous_comma(text: str) -> bool:
    """Whether a typed quantity's number uses a comma that may be a thousands separator.

    Lets a caller name the comma when turning `parse_quantity`'s refusal into a message.
    """
    match = _QUANTITY.match(text)
    return match is not None and _number_has_ambiguous_comma(match.group(1))


def _number_has_ambiguous_comma(number: str) -> bool:
    """`_AMBIGUOUS_COMMA` over the mantissa, so an exponent cannot hide a thousands group."""
    mantissa = re.split(r"[eE]", number, maxsplit=1)[0]
    return _AMBIGUOUS_COMMA.fullmatch(mantissa) is not None


def parse_quantity(text: str) -> Measurement | None:
    """A free-text quantity a person typed, or `None` when it is not one.

    Returns `None` rather than raising because most input is legitimately not a quantity ("a 96-well
    plate", "pilot scale", ""). Shared by `protocols/checks.py` (plausibility bands from the
    declared scale) and `protocols/rescale.py` (the basis a protocol is scaled to).
    """
    match = _QUANTITY.match(text)
    if match is None:
        return None
    number, unit = match.groups()
    if _number_has_ambiguous_comma(number):
        return None
    try:
        return Measurement.of(float(number.replace(",", ".")), unit)
    except (UnitError, ValueError):
        return None
