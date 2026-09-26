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

## Why the ladder is generated, and why the generator is `pint`

**This module's whole history is one bug, three times, and every instance is a prefix rung present
on one ladder and absent from the other.** `M` is molar and `m` is metre, so the two families
differ only by case and a spelling only one of them registers resolves silently to that one:
`nM` was missing, so nanomolar — the working unit of potency — folded to the *nanometre*; `pM` was
then added without its length twin, so **picometre resolved to picomolar** and a 154 pm bond length
was accepted as a concentration; `µm` was registered as an exact alias of micromolar, so a particle
size was read as a concentration and did not even reach the ambiguity guard. Each fix introduced
the next, because each was a row typed by hand into a table of 48 rows and 181 spellings.

A hand-typed rung can be missing. A **generated** one cannot, which is the entire argument for what
replaced it: the prefixed rungs are a cross product of `_LADDER` with the two base units, so
concentration and length are prefixed from **one tuple, written once and read twice** — the pairing
the old table asserted in a test is now a property of the code that builds it. `pint` supplies the
prefix factors, resolves each generated spelling, and computes every conversion factor from the
definition it was given, so no factor in this module is typed by a person any more.

**It is built from a restricted definition list and never from `pint.UnitRegistry()`.** A default
registry knows furlongs, and the refusal in `parse_unit` is this module's product rather than an
inconvenience: `pint.UnitRegistry(None)` starts empty and `_PREFIX_DEFINITIONS` and `_DEFS` are
everything it is ever told. Measured against the registry this builds: `furlong`, `m/g`, `m**2`
and `2*m` are all refused.

**Every generated spelling is checked against `UnitRegistry.get_name`, which resolves a prefix and
an alias and nothing else.** `UnitRegistry.Unit` is the obvious call and is the wrong one —
measured, `Unit("m/g")` *builds* metre-per-gram and `Unit("m**2")` square metres, which is the
derived-unit algebra two paragraphs up says has no caller here. `get_name` refuses all three, and
`Unit` is called only on a canonical name this module itself wrote. `parse_unit` reaches the
registry not at all: it is a lookup over the spellings the build generated, because the registry
pint builds from these definitions answers to every prefix on every unit — `kpKa` and `centimole`
included — and "the units this domain writes down" is the narrower of the two sets.

**Four things `pint` is deliberately not asked to do.** Its `Measurement` needs the `uncertainties`
package, which is not a dependency, so `uncertainty` stays a field here — and the field's one
subtlety is not pint's either: a spread converts by the factor and never by the offset. It has no
concept of a `basis`, because `area%` against `% w/w` is a question about the sample rather than
about units. Its registry is case-sensitive, which is half of what this domain needs and not the
convenience half, so `_FOLDED` below is still first-party. And the conversion arithmetic is the two
lines in `Measurement.to`, over factors pint computed at import: doing it through `pint.Quantity`
per call would put the refusal — `pint.DimensionalityError`, whose message names pint's internal
unit names rather than the chemist's spelling — on the path every caller's `except UnitError`
guards.
"""

import re
from dataclasses import dataclass, replace
from typing import Literal, get_args

import pint
from scipy import constants as _constants

#: The base dimensions this domain writes down. Deliberately short: current, luminous intensity and
#: angle have no caller here, and a dimension nothing uses is a row nobody checks.
#:
#: **Every one of these is a `pint` *base* dimension**, declared as `[name]` in `_DEFS` below and
#: checked against this tuple at import. Nothing is derived from anything else, which is what makes
#: `mg/mL` and `M` incomparable (see `mass_concentration`) and what keeps the registry incapable of
#: the derived-unit algebra this module refuses.
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
    # exactly what a dimension of its own expresses, and declaring them to pint as their own base
    # dimensions is what makes that true of the generator as well as of the table.
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
    reference unit of each dimension is the one declared `[dimension]` in `_DEFS`, and it is an
    implementation detail rather than a claim about SI — `fraction`'s reference is the bare
    fraction, and `energy_per_amount`'s is kJ/mol, because those are what this system stores.

    **Neither number is written down.** Both are read out of the restricted `pint` registry at
    import, so a rung's factor is whatever its definition says it is rather than whatever was typed
    beside it.
    """

    symbol: str
    dimension: Dimension
    factor: float = 1.0
    offset: float = 0.0


class UnitError(ValueError):
    """A unit was unknown, or two quantities could not be compared.

    A `ValueError`, so it travels the same non-retryable path every other bad-data refusal here
    takes: a wrong unit is not a transient fault and retrying it will produce the same answer.

    **Every refusal this module makes is this type**, including the ones a `pint` call raises
    underneath: `UndefinedUnitError` and `DimensionalityError` are translated where they arise and
    never leave this module, because a caller's `except UnitError` is what decides whether a bad
    unit reaches a chemist as a message or as a traceback.
    """


# **The three constants below are read from `scipy.constants`, not transcribed.** The transcription
# is what this module's history is made of: `HARTREE_TO_KCAL` was written out three times — here as
# a truncated 2625.4996 kJ/mol, in `science/calc/thermo.py` and again in `publish/properties.py` —
# and two of the three were short enough to disagree, leaving the registry's derived kcal/mol
# 1.5e-08 relative low. A literal cannot be wrong loudly; a sourced value can.
#
# **What that costs, measured, because it is a real change to two numbers.** `scipy` 1.17.1 carries
# CODATA **2022** where the comment here said 2018, so `E_h` is 4.359744722206e-18 J rather than
# 4.3597447222071e-18 and `HARTREE_TO_KCAL` moves 627.5094740631 -> 627.5094740628974, a relative
# 3.3e-13. `ELECTRONVOLT_TO_KJ` moves 96.48533212331 -> 96.48533212331002, one ULP, because `e` and
# `N_A` are both exact under SI-2019 and only the division is inexact. `JOULE_PER_CALORIE` is
# unchanged and `==` to the literal, the thermochemical calorie being a definition rather than a
# measurement.
#
# **Neither move reaches the calculation cache**, which is the question that decides whether this
# is safe: `CALCULATION_EPOCH` rides in the key, these constants do not, and what the cache stores
# is hartrees. Every use here is a *presentation* conversion applied after a lookup
# (`science/calc/thermo.py`, `connectors/calc/compose.py`, `publish/properties.py`), so no stored
# row's identity or payload depends on them and nothing is invalidated. The change is also two
# orders of magnitude inside GFN2-xTB's error bar, so it changes no chemistry — it is taken for
# provenance, not for accuracy.
#
# **A sourced constant that can move on a dependency bump needs a pin, or the sourcing is the
# defect.** `tests/test_units.py` asserts each of these against its literal with `==`, so the next
# CODATA release that scipy ships fails the gate and is adopted deliberately, with whatever cache
# or report consequence gets argued at that point, rather than arriving inside a lockfile bump.

#: The thermochemical calorie in joules — **exact by definition**, not a measurement, so there is
#: no precision to lose and nothing to update when CODATA does. Read from `scipy.constants` anyway,
#: so that all three come from one place and none of them is the one somebody retypes.
JOULE_PER_CALORIE = _constants.calorie

#: One hartree in kcal/mol, from CODATA's `E_h` by way of `N_A` and the calorie above.
#:
#: **This is the one definition, and it is here because `core` is the layer everything may import.**
#: `science/calc/thermo.py` and `publish/properties.py` both import this name; neither spells the
#: digits any more, which is what makes "one definition" a checkable claim rather than an intention.
HARTREE_TO_KCAL = _constants.value("Hartree energy") * _constants.N_A / 1000.0 / JOULE_PER_CALORIE

#: One electronvolt in kJ/mol — `N_A·e`, with both factors exact under SI-2019, so the only
#: inexactness is the division into kilo. The registry used to carry `96.485_332`, truncated at the
#: seventh digit for no reason anybody recorded.
ELECTRONVOLT_TO_KJ = _constants.e * _constants.N_A / 1000.0


@dataclass(frozen=True, slots=True)
class _Def:
    """One unprefixed unit: what to tell `pint`, what to call it here, and which rungs it has.

    `definition` is a `pint` definition line and the **only** place a conversion factor appears.
    `symbol` is what a chemist writes and what `__str__` prints, which is not always what pint can
    be told: a pint name may not contain a space, so `log S` is `logs` to the registry and `log S`
    here. `spellings` are the further spellings pint cannot hold — every one with a space in it,
    for that same rule.
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

#: **The one tuple the concentration and length ladders are both built from.** Written once and
#: read twice, five lines apart in `_DEFS`, because every defect in this module's history was a
#: rung on one of those two ladders and not the other — and `M`/`m` differ only by case, so the
#: ladder that has the rung answers for the spelling of the ladder that does not. A rung cannot go
#: missing from one side of a cross product.
_LADDER: tuple[str, ...] = ("centi", "milli", "micro", "nano", "pico")

#: Every unit this domain writes down. One row per *unprefixed* unit: the prefixed rungs are
#: generated, which is why `nM`, `pM` and `µm` are not rows here and cannot be forgotten.
_DEFS: tuple[_Def, ...] = (
    # Dimensionless, and the ones that are dimensionless but *scaled* — which is the distinction
    # that makes "0.15% versus 1500 ppm" answerable rather than a coin toss. `fraction` is its own
    # pint base dimension rather than pint's `dimensionless`, so a bare number and a percentage stay
    # incomparable: `reconcile(0.15, "%", "")` refuses, which is what it did before pint and what a
    # column holding "0.15" rather than "0.15%" deserves.
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
    # kJ/mol is this dimension's reference, and the hartree and the electronvolt are derived from
    # the two constants above rather than restated as a third and fourth number — which is the
    # whole point of those constants. `f"{...!r}"` round-trips a float exactly, so the registry's
    # factor is bit-for-bit the product, which `tests/test_units.py` asserts with `==`.
    _Def("kJ_per_mol = [energy_per_amount] = kJ/mol = kjmol", "kJ/mol", ("kj mol-1",)),
    _Def(
        f"kcal_per_mol = {JOULE_PER_CALORIE!r} kJ_per_mol = kcal/mol = kcalmol",
        "kcal/mol",
        ("kcal mol-1",),
    ),
    _Def(f"hartree = {HARTREE_TO_KCAL * JOULE_PER_CALORIE!r} kJ_per_mol = eh = ha = au", "hartree"),
    _Def(f"electronvolt = {ELECTRONVOLT_TO_KJ!r} kJ_per_mol = eV", "eV"),
    # The two ladders that differ only by case, prefixed from the one `_LADDER` tuple. **Read those
    # two `prefixes=_LADDER` arguments as a pair**: the module's three historical defects are all
    # the case where they were not.
    _Def("molar = [concentration] = M = mol/L", "M", prefixes=_LADDER),
    _Def("meter = [length] = m = metre", "m", prefixes=_LADDER),
    _Def("angstrom = 1e-10 meter = Å = ang", "angstrom"),
    # **`% w/v` is not a fraction.** It is grams per 100 mL *by definition*, so it belongs in
    # `mass_concentration` where the conversion is exact and needs no density — 1 % w/v is
    # 10 mg/mL. As a `fraction` with factor 0.01 it silently asserted rho = 1.000 g/mL: a 2 % w/v
    # stock read 20000 ppm and compared equal to a bare 2 %, when in ethanol it is 2.53 % w/w and
    # in DMSO 1.82 %.
    _Def("mg_per_mL = [mass_concentration] = mg/mL = g/L", "mg/mL"),
    _Def("ug_per_mL = 1e-3 mg_per_mL = ug/mL = µg/mL = μg/mL = mg/L", "ug/mL"),
    _Def("percent_wv = 10 mg_per_mL = %w/v = w/v%", "% w/v", ("% w/v",)),
    _Def("g_per_mol = [molar_mass] = g/mol = da = dalton", "g/mol", ("g mol-1",)),
    # The two calibrated properties' own scales, spelled exactly as `_CALIBRATED` spells them in
    # `connectors/calc/server/tools.py`, because the ledger's unit column and this registry have to
    # agree on the string or the check is a no-op.
    _Def("log_solubility = [log_solubility] = logs = log10(mol/l)", "log S", ("log S",)),
    _Def("pKa = [acidity]", "pKa"),
)

#: Spellings that attach to a *generated* rung rather than to a unit `_DEFS` declares, keyed by the
#: rung's symbol. A pint alias attaches to a base unit and is inherited by every prefix, so there is
#: nowhere to put "the micrometre is also called a micron" — these two are that, and the build
#: refuses a key no rung generated, so deleting a rung cannot leave one dangling.
_RUNG_SPELLINGS: dict[str, tuple[str, ...]] = {
    "um": ("micron",),
    "ug": ("mcg",),
}

#: Spellings that state their own basis. **A percent is one unit and several facts**, and these are
#: the spellings in which a chemist says which: an HPLC area percent, a weight percent, a molar
#: percent. They were registered as bare aliases of `%`, so `Measurement.of(0.15, "area%").basis`
#: was `""` and an area percent compared **equal** to a weight percent — the exact thing this
#: module's docstring says a system must not do, in the field that exists to prevent it.
#:
#: Mapping the spelling to the basis is what makes the distinction survive parsing: the caller no
#: longer has to know to pass `basis=` by hand for a string that already said it.
#: **Keyed lowercase and looked up lowercased**, because `parse_unit` is case-insensitive and this
#: was not: `Area%` — the ordinary capitalisation on a chromatography printout — parsed fine, lost
#: its basis, and compared **equal** to a `% w/w` assay. The guard this map exists to arm never
#: fired for the spelling a chemist is most likely to type.
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


# The registry, and the reason there are two of them.
#
# **Unit symbols are case-sensitive, and folding them is a real hazard rather than a pedantry.**
# `M` is molar and `m` is metre; `mM` is millimolar and `mm` is millimetre. A case-insensitive
# lookup makes each of those pairs one spelling and picks whichever was registered first — so a
# limit stated in `mM` would be read as millimetres, refuse nothing, and compare against a
# concentration as though it were a length. pint is case-sensitive by default, which is why it can
# hold both ladders at once; what it has no answer for is the chemist who writes `Area%` on a
# chromatography printout.
#
# So `_UNITS` is exact and `_FOLDED` is the convenience layer, holding a lowercase spelling **only
# while it is unambiguous**. A fold claimed by two different units maps to `None`, and `parse_unit`
# refuses it by name rather than guessing — which is how `mm` behaves once both ladders exist.
_UNITS: dict[str, Unit] = {}
_FOLDED: dict[str, Unit | None] = {}

#: **`None` is the whole argument**: `pint.UnitRegistry()` loads pint's own definition file and
#: knows furlongs. This one starts empty and is told `_PREFIX_DEFINITIONS` and `_DEFS`, and nothing
#: else, ever.
_UREG: pint.UnitRegistry[float] = pint.UnitRegistry(None)


def _forms(definition: str) -> tuple[str, tuple[str, ...]]:
    """`('micro', ('u', 'µ', 'μ'))` — a definition line's name and its symbol and aliases.

    Parsed out of the line this module hands pint rather than declared beside it, so the two cannot
    disagree. The second field is the value and is skipped; a prefix's trailing `-` is dropped.
    """
    parts = [part.strip().rstrip("-") for part in definition.split("=")]
    return parts[0], tuple(parts[2:])


def _is_word(form: str) -> bool:
    """Whether a spelling takes a prefix's *name* (`millimolar`) or its *symbol* (`mM`).

    A word is spelled out, so it pairs with the prefix spelled out. `mol/L` is not a word and pairs
    with `m` to make `mmol/L`; `mol` is three letters and would read as one, which is why a unit's
    own symbol always takes the symbol form as well.
    """
    return form.isalpha() and form.islower() and len(form) > 2


def _dimension_of(name: str) -> Dimension:
    """The `Dimension` pint derived for a unit, refusing anything this module has not declared.

    Read off the registry rather than declared beside each row, so a definition line reading
    `[concentation]` cannot quietly mint a dimension that converts with nothing. pint would accept
    it; this does not.
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

    Runs once at import. Everything it writes — the dimension, the factor, the offset and the set of
    spellings — comes out of `_PREFIX_DEFINITIONS` and `_DEFS`, so a rung is a cross product rather
    than a row somebody remembered to type.
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

    # Taken as a copy and emptied as rungs claim their entries, so what is left over at the end
    # names a rung that stopped being generated. `_RUNG_SPELLINGS` itself still reads as the map it
    # documents afterwards.
    unplaced = dict(_RUNG_SPELLINGS)

    # A bare number is not a unit pint can be told about, and it must not become pint's
    # `dimensionless` — that is one dimension with `fraction`, and `0.15` would then be a `0.15 %`.
    _add(Unit("", "dimensionless"), ("", "none", "unitless", "-"))

    for definition in _DEFS:
        name, pint_forms = _forms(definition.definition)
        dimension = _dimension_of(name)
        reference = references[dimension]
        symbol_forms = (definition.symbol, *(f for f in pint_forms if not _is_word(f)))
        # A pint name is a spelling here only when a chemist would write it. `kJ_per_mol` and
        # `mg_per_mL` are not: they exist because a pint name may hold neither a slash nor a space,
        # and `parse_unit` advertising them would put this module's implementation in a chemist's
        # error message.
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
            # Every spelling pint can hold must reach this unit *through pint*, or the generator
            # and the registry it generated from disagree — which is the state this module was in
            # three times, each time as a spelling that resolved to the wrong ladder.
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

    **Refuses rather than defaulting to dimensionless.** An unknown unit silently treated as bare
    would put "0.5 furlongs" in the same column as "0.5", and every comparison downstream would
    then be arithmetic on a number whose meaning nobody can recover.

    A lookup rather than a call into pint, deliberately: the registry pint builds from these
    definitions accepts every prefix on every unit, so it answers to `kpKa` and `centimole`, and
    "the units this domain writes down" is a narrower set than "the units that registry can spell".
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
    system that dropped the distinction would compare them. A spelling that states it — `area%`,
    `% w/w`, `mol%` — fills it at parse time, and `compare` refuses two quantities whose stated
    bases disagree. An *unstated* basis is "nobody said" and never blocks a comparison, so this
    narrows what can be compared without making ordinary percentages unusable.
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

        A spelling that states its basis (`area%`, `% w/w`, `mol%`) fills `basis` when the caller
        did not pass one — see `_BASIS_SPELLINGS`. An explicit `basis=` always wins, so a caller who
        knows more than the spelling does is never overridden.
        """
        return cls(
            value=value,
            unit=parse_unit(unit),
            uncertainty=uncertainty,
            basis=basis or _BASIS_SPELLINGS.get(unit.strip().lower(), ""),
        )

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
        # **Two stated bases that disagree are not comparable**, and this is the case a dimension
        # check cannot see: an area percent and a weight percent are the same dimension, the same
        # unit and different facts. Only refused when *both* are stated — an unstated basis is
        # "nobody said", and refusing on it would make every ordinary percent incomparable.
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

    The one call a ledger makes.

    `reported` empty means the caller stated no unit. This still accepts it as "the ledger's own
    unit", and the branch is now **unreachable from production**: `report_measurement` refuses an
    unstated unit for a calibrated property before it gets here. It is kept because the refusal
    belongs to the caller that knows which properties are calibrated, and because every measurement
    stored before that control existed carries an empty unit — a backfill reading those rows is the
    second caller this branch is for.

    **The basis is checked here too, not only in `compare`.** They are the two public comparison
    entry points and this is the one a ledger actually calls; fixing one and leaving the other is
    how an area percent reaches a weight-percent column. Only two *stated* bases that disagree are
    refused — an unstated one is "nobody said" and must not block an ordinary conversion.

    Raises:
        UnitError: When `reported` is unknown, measures something `expected` does not, or states a
            basis that disagrees with `expected`'s. A pKa reported into the solubility ledger, a
            `mg/mL` where the column holds log S, or an `area%` into a `% w/w` column is refused
            here rather than becoming a residual nobody can explain.
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


#: A leading number and a trailing unit, with optional space and an optional sign/exponent.
#: Deliberately anchored at both ends: this reads a field a *person* wrote as the whole answer
#: ("20 kg"), not a number mentioned inside a sentence. `quantities.labelled_values` is the tool for
#: the other job, and conflating them would make "run it at 20 °C in 500 mL" parse as a scale.
_QUANTITY = re.compile(r"^\s*([+-]?\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?)\s*([^\s\d].*?)\s*$")

#: A comma that could be a thousands separator: one to three digits not starting with 0, then a
#: comma and exactly three digits. "1,500" is 1500 to one reader and 1.5 to another. Refused rather
#: than guessed, because either guess is a factor of 1000 in a rescaled protocol and the recorded
#: basis string would still read "1,500" — the error would be invisible where it lands. "0,500"
#: is not ambiguous (no thousands group starts with 0), so it reads as a decimal. Matched against
#: the mantissa only, so "1,500e3" is refused too.
_AMBIGUOUS_COMMA = re.compile(r"[+-]?[1-9]\d{0,2},\d{3}")


def has_ambiguous_comma(text: str) -> bool:
    """Whether a typed quantity's number uses a comma that may be a thousands separator.

    `parse_quantity` refuses such a number, and a caller turning that refusal into a message
    should name the comma rather than say "not a quantity" about something that plainly is one.
    """
    match = _QUANTITY.match(text)
    return match is not None and _number_has_ambiguous_comma(match.group(1))


def _number_has_ambiguous_comma(number: str) -> bool:
    """`_AMBIGUOUS_COMMA` over the mantissa, so an exponent cannot hide a thousands group."""
    mantissa = re.split(r"[eE]", number, maxsplit=1)[0]
    return _AMBIGUOUS_COMMA.fullmatch(mantissa) is not None


def parse_quantity(text: str) -> Measurement | None:
    """A free-text quantity a person typed, or `None` when it is not one.

    **Returns `None` rather than raising, because most of what reaches it is legitimately not a
    quantity.** `ExperimentRequest.scale` is a `RequestField` whose value is whatever the chemist
    said, and "a 96-well plate", "pilot scale" and "" are all ordinary answers. A parser that
    raised on those would make every caller write the same `try`, and the second caller would write
    it differently.

    Two callers, which is why this is here rather than inlined: `protocols/checks.py` derives the
    plausibility bands from the declared scale, and `protocols/rescale.py` reads the basis a
    protocol is being scaled to. Both want the same three answers — a number, its dimension, or
    "that was not a quantity" — and both must not explode on prose.
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
