"""A quantity with a dimension, and the comparisons that refuse.

`record_observation` stores a `unit` beside every measured value, so `0.5` log S and `0.5` mg/mL
must be distinguishable. Three groups: conversions checked against independently written numbers,
refusals checked in both directions, and the wiring where the ledger's unit meets the reported one.
"""

import pytest

from chemclaw.core.units import (
    ELECTRONVOLT_TO_KJ,
    HARTREE_TO_KCAL,
    JOULE_PER_CALORIE,
    Measurement,
    UnitError,
    has_ambiguous_comma,
    parse_quantity,
    parse_unit,
    reconcile,
)


def _same_dimension(first: str, second: str) -> bool:
    """The dimension comparison `Measurement.to` and `Measurement.compare` make, spelled out."""
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
    # A hartree is 627.5 kcal/mol, the figure a computational chemist knows, rather than the stored
    # kJ/mol value. Precision and the single-definition rule are pinned in
    # `test_the_energy_ladder_carries_the_full_codata_value_and_one_definition`.
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

    A case-insensitive registry would merge each pair and read a limit in `mM` as a length.
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
    """Reconcile accepts an unstated unit and refuses a wrong one.

    An unstated unit means the ledger's own, since older measurements carry an empty one; a number
    in the wrong unit silently stored is what this catches.
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

    Imported from the server module so the assertion is against the real table.
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
    """Every rung of the concentration ladder is a concentration.

    Case separates molarity from length only where both families register the same prefix; a rung on
    one side alone resolves to the other dimension (`nM` to nanometre). Asserted over the whole
    ladder.
    """
    for spelling, factor in (("M", 1.0), ("mM", 1e-3), ("uM", 1e-6), ("nM", 1e-9), ("pM", 1e-12)):
        unit = parse_unit(spelling)
        assert unit.dimension == "concentration", f"{spelling} resolved to {unit.dimension}"
        assert unit.factor == pytest.approx(factor)


def test_every_rung_of_the_length_ladder_is_a_length_including_the_micro_signs() -> None:
    """Every rung of the length ladder is a length, including the micro signs.

    Both U+00B5 and Greek mu U+03BC are checked, since a chemist may type either.
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
    """A percent that states its basis carries it and will not compare across bases.

    `area%` and `% w/w` fill `basis`, and two stated bases that disagree refuse.
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
    """No prefix is registered on one ladder and not the other.

    A rung present on one side alone silently resolves to the family that has it (e.g. `pm`, a bond
    length unit, read as picomolar). Derived from the registry rather than a hand-written list.
    """
    from chemclaw.core.units import _UNITS

    #: Folds that exist on one ladder because the other reading is not a real unit. Every other rung
    #: has a counterpart, because both ladders are prefixed from one tuple; generating a rung is
    #: cheaper than arguing for omitting it.
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
    """`reconcile` refuses a basis mismatch the way `compare` does.

    `reconcile` is the path that writes to the calibration ledger, so both entry points must agree.
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
    """The energy ladder carries the full CODATA value and one definition.

    A conversion factor is a defined constant, so it is checked by exact equality against the one
    definition and at full precision against independently written references (CODATA 2018 / SI
    2019, none read off `core/units.py`): E_h = 4.3597447222071e-18 J, N_A = 6.02214076e23 /mol,
    e = 1.602176634e-19 C (the last two exact), and the thermochemical calorie is 4.184 J exactly.
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

    # One definition: the registry's factor derives from `HARTREE_TO_KCAL`. Asserted on the factor
    # rather than a round trip through `to()`, which could agree by floating-point coincidence.
    assert parse_unit("hartree").factor == HARTREE_TO_KCAL * JOULE_PER_CALORIE
    assert parse_unit("kcal/mol").factor == JOULE_PER_CALORIE
    assert Measurement.of(1.0, "hartree").to("kcal/mol").value == pytest.approx(
        HARTREE_TO_KCAL, rel=1e-15
    )


def test_the_two_ladders_that_differ_only_by_case_are_prefixed_from_one_tuple() -> None:
    """The two case-differing ladders are prefixed from one tuple object.

    The test above checks that the current table agrees; this checks why it cannot disagree.
    Identity rather than equality, so a copied tuple fails.
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
    """`nM`, `pm` and `µm` each resolve to their own ladder.

    The specific cross-ladder confusions the sweep above guards against.
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
    """The registry is a restricted chemistry registry, not the unit library's default.

    Refusing unknown units is the product, so it is `pint.UnitRegistry(None)` plus exactly
    `_PREFIX_DEFINITIONS` and `_DEFS`. There is no derived-unit algebra: resolution goes through
    `get_name`, which builds nothing, so `m/g` or `m**2` are refused.
    """
    for unknown in ("furlong", "nautical_mile", "psi", "degF", "mol/kg"):
        with pytest.raises(UnitError, match="unknown unit"):
            parse_unit(unknown)
    for expression in ("m/g", "m**2", "2*m", "kg*m", "mol/L/s"):
        with pytest.raises(UnitError):
            parse_unit(expression)


def test_no_exception_from_the_unit_library_reaches_a_caller() -> None:
    """No exception from the unit library reaches a caller; every refusal is `UnitError`.

    `pint.UndefinedUnitError` is an `AttributeError` and `DimensionalityError` a `TypeError`;
    `UnitError` is a `ValueError`, the non-retryable bad-data path, and an escaping `AttributeError`
    could be swallowed far from its cause.
    """
    import pint

    for probe in (lambda: parse_unit("furlong"), lambda: Measurement.of(1.0, "mg").to("mL")):
        with pytest.raises(UnitError) as caught:
            probe()
        assert not isinstance(caught.value, pint.PintError)
        assert isinstance(caught.value, ValueError)


def test_the_three_physical_constants_are_pinned_against_the_codata_release_scipy_ships() -> None:
    """The physical constants are pinned against the CODATA release scipy ships.

    `core/units.py` reads them from `scipy.constants`, so a dependency bump could move them
    silently. Compared with `==`, since a new CODATA release is a new number and the point is to be
    told. The calorie is exact by definition; its arm guards against scipy renaming or re-basing the
    attribute.
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
    """`parse_quantity` returns `None` for text that is not a quantity, rather than raising.

    `ExperimentRequest.scale` holds free text such as "a 96-well plate", and every caller would
    otherwise need its own `try`.
    """
    assert parse_quantity(text) is None


def test_parse_quantity_refuses_a_number_inside_a_sentence() -> None:
    """Anchored at both ends: this reads a field, not prose that mentions a number.

    "run it at 20 °C in 500 mL" parsing as a 20 °C *scale* is the failure the anchors prevent, and
    `quantities.labelled_values` is the tool for the other job.
    """
    assert parse_quantity("run it at 20 C in 500 mL") is None


@pytest.mark.parametrize(
    "text", ["1,000 g", "1,500 mL", "12,345 mg", "-1,000 g", "1,500e3 g", "+1,500E-3 mol"]
)
def test_parse_quantity_refuses_a_comma_that_may_be_a_thousands_separator(text: str) -> None:
    """A comma followed by exactly three digits is refused, not read as a decimal.

    "1,500 g" read as 1.5 g would scale a protocol a thousandfold too small. An exponent does not
    hide the group: "1,500e3" is ambiguous too.
    """
    assert parse_quantity(text) is None
    assert has_ambiguous_comma(text)


@pytest.mark.parametrize(
    ("text", "value", "unit"),
    [("0,500 g", 0.5, "g"), ("-0,250 mol", -0.25, "mol"), ("1,5 g", 1.5, "g")],
)
def test_parse_quantity_reads_a_comma_that_cannot_be_a_thousands_separator(
    text: str, value: float, unit: str
) -> None:
    """No thousands group starts with 0 or has fewer than three digits: those are decimals."""
    parsed = parse_quantity(text)
    assert parsed is not None
    assert parsed.value == pytest.approx(value)
    assert parsed.unit.symbol == unit
    assert not has_ambiguous_comma(text)
