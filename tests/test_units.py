"""A quantity with a dimension, and the comparisons that refuse.

The gap this closes is one column. `infra/sql/030_measurements.sql` declares
`unit TEXT NOT NULL DEFAULT ''`, `record_observation` has always taken a `unit` argument and written
it through unexamined, and `report_measurement` never passed one — so every measured value this
system has stored carries an empty unit, and `0.5` was stored identically whether the chemist meant
log S or mg/mL.

Three groups below. The conversions are arithmetic and are checked against independently written
numbers. The refusals are the point of the module and are checked in both directions. The last group
is the wiring: the ledger's unit and the reported unit have to meet somewhere, and this is where.
"""

import pytest

from chemclaw.core.units import (
    ELECTRONVOLT_TO_KJ,
    HARTREE_TO_KCAL,
    JOULE_PER_CALORIE,
    Measurement,
    UnitError,
    parse_quantity,
    parse_unit,
    reconcile,
)


def _same_dimension(first: str, second: str) -> bool:
    """The comparison `Measurement.to` and `Measurement.compare` make, spelled out.

    Here rather than in `core/units.py`, where it was a public function whose only caller was this
    file: the dimension table is what the refusals below rest on, and the surface that reads it in
    production is `parse_unit(...).dimension` on those two methods.
    """
    return parse_unit(first).dimension == parse_unit(second).dimension


def test_a_conversion_is_the_number_a_chemist_would_write() -> None:
    """Each expected value is written independently of the factor table it checks."""
    # 1500 ppm is 0.15%: both are fractions, three orders apart.
    assert Measurement.of(1500, "ppm").to("%").value == pytest.approx(0.15)
    # Water's freezing point, the one conversion an offset is needed for.
    assert Measurement.of(0.0, "degC").to("K").value == pytest.approx(273.15)
    assert Measurement.of(25.0, "degC").to("K").value == pytest.approx(298.15)
    # A kcal is 4.184 kJ by definition.
    assert Measurement.of(1.0, "kcal/mol").to("kJ/mol").value == pytest.approx(4.184)
    # A hartree is 627.5 kcal/mol — the figure a computational chemist knows by heart, which is
    # why it is the one asserted rather than the 2625.5 kJ/mol the table stores. **Asserted to the
    # full value, not to `rel=1e-3`**: that tolerance was a band of [626.87, 628.13], so a drift of
    # ±0.63 kcal/mol per hartree — a whole reaction ΔG — passed the guard that exists to catch it.
    # `test_the_energy_ladder_carries_the_full_codata_value_and_one_definition` is where the
    # precision and the single-definition rule are actually pinned.
    assert Measurement.of(1.0, "hartree").to("kcal/mol").value == pytest.approx(
        627.5094740631, rel=1e-12
    )
    assert Measurement.of(2.0, "h").to("min").value == pytest.approx(120.0)


def test_an_uncertainty_is_scaled_but_never_offset() -> None:
    """A spread of 2 °C is a spread of 2 K.

    The classic temperature bug, in the one field nobody re-reads: adding 273.15 to an uncertainty
    turns a tight measurement into a meaningless one while the value beside it stays correct.
    """
    converted = Measurement.of(25.0, "degC", uncertainty=2.0).to("K")
    assert converted.value == pytest.approx(298.15)
    assert converted.uncertainty == pytest.approx(2.0)
    # And it *is* scaled where a factor applies: 0.5 g is 500 mg, ± 20 mg.
    grams = Measurement.of(0.5, "g", uncertainty=0.02).to("mg")
    assert grams.value == pytest.approx(500.0)
    assert grams.uncertainty == pytest.approx(20.0)


def test_no_uncertainty_is_not_zero_uncertainty() -> None:
    """`None` survives a conversion, because zero is a claim of exactness and silence is not."""
    assert Measurement.of(1.0, "g").to("mg").uncertainty is None


def test_comparing_across_dimensions_refuses_rather_than_ordering_floats() -> None:
    """The refusal is the whole module.

    Python would happily order two floats whose units disagree, and a specification check written
    that way passes a batch that is out of limits.
    """
    # A purity in percent against an amount in milligrams: two numbers Python would order.
    with pytest.raises(UnitError, match="not the same kind of quantity"):
        Measurement.of(0.15, "%").compare(Measurement.of(1.5, "mg"))

    # Within a dimension it orders correctly, and 1500 ppm *is* 0.15%.
    assert Measurement.of(0.15, "%").compare(Measurement.of(1500, "ppm")) == 0
    assert Measurement.of(0.2, "%").compare(Measurement.of(1500, "ppm")) == 1
    assert Measurement.of(0.1, "%").compare(Measurement.of(1500, "ppm")) == -1


def test_a_fraction_and_a_ppm_are_the_same_dimension_and_a_mass_is_not() -> None:
    """The dimension table is what makes the refusals above land where they should."""
    assert _same_dimension("%", "ppm")
    assert _same_dimension("mg", "kg")
    assert not _same_dimension("%", "mg")
    assert not _same_dimension("M", "mg/mL")


def test_molarity_and_mass_concentration_are_deliberately_not_convertible() -> None:
    """Turning mg/mL into M needs the molar mass, which is a fact about the sample.

    This module has no algebra over derived units precisely so that it cannot invent one. The
    refusal is the right answer: the conversion needs an input nobody supplied.
    """
    with pytest.raises(UnitError, match="different things"):
        Measurement.of(0.5, "mg/mL").to("mM")
    # Within mass concentration it converts: 0.5 mg/mL is 500 µg/mL.
    assert Measurement.of(0.5, "mg/mL").to("ug/mL").value == pytest.approx(500.0)


def test_case_is_significant_and_an_ambiguous_fold_is_refused() -> None:
    """`M` is molar and `m` is metre; `mM` is millimolar and `mm` is millimetre.

    A case-insensitive registry makes each pair one spelling and answers with whichever was
    registered first — so a limit in `mM` would be read as millimetres, refuse nothing, and compare
    against a concentration as though it were a length. Measured while building this: a folded
    registry could not hold molarity and length at once.
    """
    assert parse_unit("M").dimension == "concentration"
    assert parse_unit("m").dimension == "length"
    assert parse_unit("mM").dimension == "concentration"
    assert parse_unit("mm").dimension == "length"
    # A spelling two units claim once folded is refused by name rather than guessed at.
    with pytest.raises(UnitError, match="ambiguous"):
        parse_unit("MM")


def test_an_unknown_unit_refuses_rather_than_defaulting_to_dimensionless() -> None:
    """Silently treating an unknown unit as bare puts "0.5 furlongs" in the same column as "0.5"."""
    with pytest.raises(UnitError, match="unknown unit"):
        parse_unit("furlong")


def test_a_log_scale_is_its_own_dimension() -> None:
    """Nothing converts into log S, and pKa is not log S.

    Both carry no units and calling them "dimensionless" would make them interconvertible with each
    other and with `%` — so a pKa reported into the solubility ledger would be accepted silently.
    """
    assert not _same_dimension("log S", "pKa")
    assert not _same_dimension("log S", "")
    with pytest.raises(UnitError):
        Measurement.of(4.7, "pKa").to("log S")


def test_reconcile_accepts_an_unstated_unit_and_refuses_a_wrong_one() -> None:
    """The ledger's one call.

    An unstated unit means "the ledger's own", because every measurement stored before this existed
    carries an empty one and refusing the unstated case would break the common path while the
    *wrong* path — a number in the wrong unit, silently stored — is what this exists to catch.
    """
    assert reconcile(0.5, "", "log S") == 0.5
    assert reconcile(0.5, "log S", "log S") == 0.5
    # 1500 ppm into a ledger holding %, converted rather than refused.
    assert reconcile(1500, "ppm", "%") == pytest.approx(0.15)
    for wrong in ("mg/mL", "pKa", "%"):
        with pytest.raises(UnitError):
            reconcile(0.5, wrong, "log S")


def test_the_ledgers_two_properties_are_spelled_the_same_here_as_there() -> None:
    """`_CALIBRATED`'s unit strings must parse, or the check `report_measurement` makes is inert.

    The one coupling worth a test: this registry and that table agree on a string, and if they ever
    stop agreeing the reconciliation raises `unknown unit` on every reported measurement instead of
    checking one. Imported from the server module so the assertion is against the real table.
    """
    from chemclaw.connectors.calc.server.tools import _CALIBRATED

    for property_name, (_tool, unit) in _CALIBRATED.items():
        assert parse_unit(unit), f"{property_name!r} declares unit {unit!r}, which does not parse"


def test_str_reads_the_way_a_chemist_writes_it() -> None:
    """`1.5 ± 0.05 mg (area%)` — value, spread, unit, and what it is a fraction of."""
    assert str(Measurement.of(1.5, "mg")) == "1.5 mg"
    assert str(Measurement.of(1.5, "mg", uncertainty=0.05)) == "1.5 ± 0.05 mg"
    assert str(Measurement.of(0.15, "%", basis="area")) == "0.15 % (area)"
    # A dimensionless value prints as a bare number rather than with an empty unit appended.
    assert str(Measurement.of(4.7, "")) == "4.7"


def test_every_rung_of_the_concentration_ladder_is_a_concentration() -> None:
    """The defect this file's original tests could not see, because they stopped at `mM`.

    Case is what separates molarity from length here, and that only works where both families
    register the same prefix — a fold claimed by two units is poisoned and the ambiguous spelling is
    refused. `M`/`m` and `mM`/`mm` were both present, so the mechanism looked proven; `nM` was not,
    so nanomolar resolved to the *nanometre* sitting alone on that rung. Nanomolar is the working
    unit of potency, so this was not an edge case: an IC50 could be ordered against a particle size.

    Asserted over the whole ladder rather than one more example, since one more example is exactly
    what the original test was.
    """
    for spelling, factor in (("M", 1.0), ("mM", 1e-3), ("uM", 1e-6), ("nM", 1e-9), ("pM", 1e-12)):
        unit = parse_unit(spelling)
        assert unit.dimension == "concentration", f"{spelling} resolved to {unit.dimension}"
        assert unit.factor == pytest.approx(factor)


def test_every_rung_of_the_length_ladder_is_a_length_including_the_micro_signs() -> None:
    """The same in the other direction — and `µm` is where it went wrong.

    `µm` was registered as an *exact* alias of micromolar, so it did not even reach the ambiguity
    guard: micrometre was absent from the length family, nothing poisoned the fold, and a 50 µm
    particle size was accepted as a concentration. Both micro signs are checked, because the micro
    sign (U+00B5) and the Greek mu (U+03BC) are different code points a chemist may type either of.
    """
    for spelling in ("m", "cm", "mm", "um", "µm", "μm", "nm", "pm"):
        assert parse_unit(spelling).dimension == "length", f"{spelling} is not a length"
    # And the correctly-spelled micromolar still reaches micromolar, which is all those aliases
    # were ever needed for.
    for spelling in ("uM", "µM", "μM"):
        assert parse_unit(spelling).dimension == "concentration"


def test_a_potency_cannot_be_ordered_against_a_particle_size() -> None:
    """The failure the ladders exist to prevent, stated as the outcome rather than as a lookup."""
    with pytest.raises(UnitError):
        Measurement.of(50, "nM").compare(Measurement.of(1, "mm"))
    with pytest.raises(UnitError):
        reconcile(50, "µm", "mM")
    # And the comparison that must *work* — two concentrations two rungs apart.
    assert Measurement.of(50, "nM").compare(Measurement.of(0.1, "uM")) == -1


def test_a_percent_that_states_its_basis_carries_it_and_will_not_compare_across_bases() -> None:
    """An area percent and a weight percent are one unit and two facts.

    The module docstring says a system that dropped the distinction would compare them; `area%` and
    `% w/w` were bare aliases of `%`, so `basis` was empty and they compared **equal**. A spelling
    that states the basis now fills it, and two *stated* bases that disagree refuse.
    """
    area = Measurement.of(0.15, "area%")
    weight = Measurement.of(0.15, "% w/w")
    assert (area.basis, weight.basis) == ("area", "w/w")
    with pytest.raises(UnitError):
        area.compare(weight)
    # An unstated basis is "nobody said" and must not block an ordinary comparison.
    assert Measurement.of(0.15, "%").compare(area) == 0
    # An explicit basis always wins over the spelling's.
    assert Measurement.of(0.15, "area%", basis="w/w").basis == "w/w"


def test_no_prefix_is_registered_on_one_ladder_and_not_the_other() -> None:
    """Derived from the registry, because a hand-written list is what missed this twice.

    Case is what separates molarity from length, and it only works where **both** families register
    the same rung: a fold claimed by two units is poisoned and the ambiguous spelling refuses.
    A rung present on one side alone silently resolves to whichever family has it.

    That defect was found, fixed for `nM`/`µm` — and reintroduced in the same commit. The fix
    added `pM` with a comment calling it "unambiguous" for having no length twin; the missing twin
    is exactly what made it dangerous, and `pm` — the unit of a bond length — resolved to picomolar.
    `reconcile(154, "pm", "M")` returned 1.54e-10 where `origin/main` had refused it outright. Both
    times the test enumerated spellings by hand and stopped one rung short.

    So this asks the registry. The allowlist is one entry now, and the entry it lost is the point:
    see the comment on it.
    """
    from chemclaw.core.units import _UNITS

    #: Folds that exist on one ladder because the other reading is not a real unit. An
    #: "angstrom-molar" is not written by anybody; every other rung of both ladders now has a
    #: counterpart, because the two ladders are prefixed from one tuple rather than from two lists.
    #: **`cm` used to be the second entry here** and is not any more: centimolar is indeed not
    #: written by anybody, but its absence is what made `parse_unit("cM")` answer *centimetre* —
    #: a fourth instance of this module's one defect, sitting inside the allowlist written to
    #: excuse it. A rung that costs nothing to generate is cheaper than an argument for omitting it.
    exempt_folds = {"angstrom"}

    def folds(dimension: str) -> set[str]:
        return {
            symbol.lower()
            for symbol, unit in _UNITS.items()
            if unit.dimension == dimension and symbol == unit.symbol
        }

    unpaired = (folds("concentration") ^ folds("length")) - exempt_folds
    assert unpaired == set(), (
        f"{sorted(unpaired)} exist on one of the concentration/length ladders and not the "
        "other, so each resolves silently to whichever family has it instead of refusing"
    )


def test_reconcile_refuses_a_basis_mismatch_the_way_compare_does() -> None:
    """The two public comparison entry points must agree, and only one had the check.

    `reconcile` is the one a ledger actually calls (`report_measurement` goes through it), and it
    consulted `basis` not at all — so the exact mismatch `compare` was taught to refuse was silently
    permitted on the path that writes to the calibration ledger. Fixing one and leaving its sibling
    is how an area percent reaches a weight-percent column.
    """
    with pytest.raises(UnitError):
        reconcile(0.15, "area%", "% w/w")
    # Capitalised too, because `parse_unit` is case-insensitive and the basis map now is as well.
    with pytest.raises(UnitError):
        reconcile(0.15, "Area%", "% W/W")
    # An unstated basis on either side is "nobody said" and must not block an ordinary conversion.
    assert reconcile(0.15, "area%", "%") == pytest.approx(0.15)
    assert reconcile(0.15, "%", "area%") == pytest.approx(0.15)


def test_the_energy_ladder_carries_the_full_codata_value_and_one_definition() -> None:
    """The guard this replaces admitted ±0.63 kcal/mol per hartree, which is a whole reaction ΔG.

    `approx(627.5, rel=1e-3)` is an admissible band of [626.87, 628.13]: every drift a wrong
    hartree could introduce fits inside it, so the assertion that exists to pin the conversion
    could not have failed for any error a chemist would notice. A conversion factor is a defined
    constant, not a measurement, so the tolerance on it is the tolerance of the arithmetic —
    exact equality against the one definition, and full CODATA precision against a figure written
    independently of the table it checks.

    Independent references, CODATA 2018 / SI 2019 (none of them read off `core/units.py`):
    E_h = 4.3597447222071e-18 J, N_A = 6.02214076e23 /mol, e = 1.602176634e-19 C (the last two
    exact), and the thermochemical calorie is 4.184 J exactly.
    """
    hartree_kj_per_mol = 4.3597447222071e-18 * 6.02214076e23 / 1000.0  # 2625.4996394798...
    electronvolt_kj_per_mol = 1.602176634e-19 * 6.02214076e23 / 1000.0  # 96.4853321233...

    assert Measurement.of(1.0, "hartree").to("kJ/mol").value == pytest.approx(
        hartree_kj_per_mol, rel=1e-12
    )
    assert Measurement.of(1.0, "hartree").to("kcal/mol").value == pytest.approx(
        hartree_kj_per_mol / 4.184, rel=1e-12
    )
    assert Measurement.of(1.0, "eV").to("kJ/mol").value == pytest.approx(
        electronvolt_kj_per_mol, rel=1e-12
    )

    # And **one** definition: the registry's factor is derived from `HARTREE_TO_KCAL` rather than
    # restating it, so the two cannot drift apart the way three independent literals did. Asserted
    # on the factor itself rather than on the round trip through `to()`, because `(x*c)/c == x` is a
    # floating-point coincidence and this is a statement about where the number comes from.
    assert parse_unit("hartree").factor == HARTREE_TO_KCAL * JOULE_PER_CALORIE
    assert parse_unit("kcal/mol").factor == JOULE_PER_CALORIE
    assert Measurement.of(1.0, "hartree").to("kcal/mol").value == pytest.approx(
        HARTREE_TO_KCAL, rel=1e-15
    )


def test_the_two_ladders_that_differ_only_by_case_are_prefixed_from_one_tuple() -> None:
    """The mechanism, not another example of it — because examples are what kept missing a rung.

    Every defect in this module's history is one shape: a prefix rung present on the concentration
    ladder and absent from the length one, or the reverse. `M` and `m` differ only by case, so the
    ladder that has the rung answers for the spelling of the ladder that does not, and the answer is
    a plausible number in the wrong dimension.

    The test above this one asks the built registry whether the ladders agree, which is a statement
    about the current table. This one asks *why they cannot disagree*: both rows hold the same tuple
    object, so a rung added to one is added to the other in the same keystroke. Identity rather than
    equality on purpose — two equal tuples written out separately are exactly the arrangement that
    failed three times, and `is` is what makes copying one of them fail here.
    """
    from chemclaw.core.units import _DEFS, _LADDER, _UNITS

    ladders = {row.symbol: row.prefixes for row in _DEFS if row.prefixes is _LADDER}
    assert ladders.keys() == {"M", "m"}, (
        "the concentration and length ladders are the pair that differ only by case; "
        f"_LADDER is shared by {sorted(ladders)} instead"
    )
    # And the rungs really are that tuple applied to both symbols, rather than a table that happens
    # to agree with it today.
    for symbol, dimension in (("M", "concentration"), ("m", "length")):
        rungs = {unit.symbol for unit in _UNITS.values() if unit.dimension == dimension}
        assert {f"{prefix[0]}{symbol}" for prefix in _LADDER} | {symbol} <= rungs


def test_the_three_defects_this_registry_was_rebuilt_to_make_impossible() -> None:
    """`nM`, `pm` and `µm`, each of which once resolved to the other ladder.

    Stated as the three original bug reports rather than as a sweep, because the sweep is the test
    above and this is the record of what it is sweeping for. Each of these shipped: `nM` folded to
    the nanometre, `pm` — the unit of a bond length — resolved to picomolar once `pM` was added
    without its twin, and `µm` was registered as an exact alias of micromolar, so a particle size
    was accepted as a concentration without even reaching the ambiguity guard.
    """
    assert parse_unit("nM").dimension == "concentration"
    assert parse_unit("nm").dimension == "length"
    assert parse_unit("pM").dimension == "concentration"
    assert parse_unit("pm").dimension == "length"
    for micro in ("µm", "μm", "um"):
        assert parse_unit(micro).dimension == "length", micro
    for micro in ("µM", "μM", "uM"):
        assert parse_unit(micro).dimension == "concentration", micro
    # The fourth instance, found by generating the ladders instead of listing them: `cM` reached
    # the *centimetre* through the case fold, because centimolar was the rung nobody wrote down.
    assert parse_unit("cM").dimension == "concentration"
    with pytest.raises(UnitError, match="ambiguous"):
        parse_unit("CM")


def test_the_registry_is_this_domains_and_not_the_unit_librarys() -> None:
    """A chemistry registry, built from a restricted definition list rather than from the default.

    The refusal in `parse_unit` is this module's product. `pint.UnitRegistry()` would accept
    furlongs, nautical miles and everything else — so the registry is `pint.UnitRegistry(None)`,
    which starts empty, plus exactly what `_PREFIX_DEFINITIONS` and `_DEFS` say.

    The second half is the one a unit library makes easy to lose: **there is no algebra over derived
    units here**, because nothing in this system multiplies a mass by a length, and a registry that
    could would be an abstraction with no caller. `UnitRegistry.Unit("m/g")` builds metre-per-gram
    and `Unit("m**2")` builds square metres, which is why resolution goes through `get_name` — a
    prefix-and-alias lookup that builds nothing.
    """
    for unknown in ("furlong", "nautical_mile", "psi", "degF", "mol/kg"):
        with pytest.raises(UnitError, match="unknown unit"):
            parse_unit(unknown)
    for expression in ("m/g", "m**2", "2*m", "kg*m", "mol/L/s"):
        with pytest.raises(UnitError):
            parse_unit(expression)


def test_no_exception_from_the_unit_library_reaches_a_caller() -> None:
    """Every refusal is `UnitError`, or every caller's `except` clause means something else.

    `pint.UndefinedUnitError` is an **`AttributeError`** and `pint.DimensionalityError` is a
    `TypeError`; `UnitError` is a `ValueError`, which is the non-retryable bad-data path the rest of
    this tree takes. Letting either through would not merely change a type — an `AttributeError`
    escaping into `chemclaw.analytical` is the kind `hasattr` and a bare `except AttributeError`
    swallow somewhere far away from the wrong unit that caused it.
    """
    import pint

    for probe in (lambda: parse_unit("furlong"), lambda: Measurement.of(1.0, "mg").to("mL")):
        with pytest.raises(UnitError) as caught:
            probe()
        assert not isinstance(caught.value, pint.PintError)
        assert isinstance(caught.value, ValueError)


def test_the_three_physical_constants_are_pinned_against_the_codata_release_scipy_ships() -> None:
    """A sourced constant that can move on a dependency bump needs a pin, or sourcing is the defect.

    `core/units.py` reads these from `scipy.constants` rather than transcribing them, which is the
    right trade — a literal is what let one hartree be written out three times and disagree twice.
    But it hands a third party the value: `scipy` 1.17.1 carries CODATA **2022** where this module's
    comment used to say 2018, and adopting it moved `HARTREE_TO_KCAL` by a relative 3.3e-13 on its
    own. That move was argued; the next one would arrive inside a lockfile bump with nobody asked.

    So the literals here are the pin, compared with `==` rather than a tolerance: a CODATA release
    *is* a new number, however small, and the point is to be told. The consequences to weigh when
    this fails are in `core/units.py`'s comment — measured, none of them reaches the calculation
    cache, because `CALCULATION_EPOCH` rides in the key and these do not.

    `JOULE_PER_CALORIE` is here for a different reason and cannot fail for the same one: the
    thermochemical calorie is exact by definition, so this arm is a guard against `scipy.constants`
    renaming or re-basing the attribute rather than against a measurement improving.
    """
    assert JOULE_PER_CALORIE == 4.184
    assert HARTREE_TO_KCAL == 627.5094740628974
    assert ELECTRONVOLT_TO_KJ == 96.48533212331002


# --- parse_quantity -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value", "symbol"),
    [
        ("20 kg", 20.0, "kg"),
        ("20kg", 20.0, "kg"),
        ("  500 mg  ", 500.0, "mg"),
        ("1.5 L", 1.5, "L"),
        ("2,5 kg", 2.5, "kg"),
        ("2,5 mmol", 2.5, "mmol"),
        ("1,25 L", 1.25, "L"),
        ("1e3 g", 1000.0, "g"),
        ("0.5 mol", 0.5, "mol"),
    ],
)
def test_parse_quantity_reads_what_a_person_types_in_a_scale_field(
    text: str, value: float, symbol: str
) -> None:
    """Including the comma decimal and the missing space, because both are what people write."""
    quantity = parse_quantity(text)
    assert quantity is not None
    assert quantity.value == pytest.approx(value)
    assert quantity.unit.symbol == symbol


@pytest.mark.parametrize(
    "text", ["", "   ", "a 96-well plate", "pilot scale", "20 furlongs", "kg", "lots", "20"]
)
def test_parse_quantity_returns_none_for_what_is_not_a_quantity(text: str) -> None:
    """`None` rather than an exception, because most of what reaches it is legitimately prose.

    `ExperimentRequest.scale` holds whatever the chemist said. A parser that raised on "a 96-well
    plate" would put the same `try` in every caller, and the second caller would write it
    differently — which is the whole reason this returns an answer instead of a failure.
    """
    assert parse_quantity(text) is None


def test_parse_quantity_refuses_a_number_inside_a_sentence() -> None:
    """Anchored at both ends: this reads a field, not prose that mentions a number.

    "run it at 20 °C in 500 mL" parsing as a 20 °C *scale* is the failure the anchors prevent, and
    `quantities.labelled_values` is the tool for the other job.
    """
    assert parse_quantity("run it at 20 C in 500 mL") is None


@pytest.mark.parametrize("text", ["1,000 g", "1,500 mL", "12,345 mg", "-1,000 g"])
def test_parse_quantity_refuses_a_comma_that_may_be_a_thousands_separator(text: str) -> None:
    """A comma and exactly three digits is refused, not read as a decimal.

    "1,500 g" read as 1.5 g scales a protocol a thousand times too small while its recorded basis
    still says "1,500 g", so the caller's "write it as a number and a unit" refusal is the answer.
    """
    assert parse_quantity(text) is None
