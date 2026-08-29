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

from chemclaw.core.units import Measurement, UnitError, parse_unit, reconcile, same_dimension


def test_a_conversion_is_the_number_a_chemist_would_write() -> None:
    """Each expected value is written independently of the factor table it checks."""
    # 1500 ppm is 0.15%: both are fractions, three orders apart.
    assert Measurement.of(1500, "ppm").to("%").value == pytest.approx(0.15)
    # Water's freezing point, the one conversion an offset is needed for.
    assert Measurement.of(0.0, "degC").to("K").value == pytest.approx(273.15)
    assert Measurement.of(25.0, "degC").to("K").value == pytest.approx(298.15)
    # A kcal is 4.184 kJ by definition.
    assert Measurement.of(1.0, "kcal/mol").to("kJ/mol").value == pytest.approx(4.184)
    # A hartree is ~627.5 kcal/mol — the figure a computational chemist knows by heart, which is
    # why it is the one asserted rather than the 2625.5 kJ/mol the table stores.
    assert Measurement.of(1.0, "hartree").to("kcal/mol").value == pytest.approx(627.5, rel=1e-3)
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
    assert same_dimension("%", "ppm")
    assert same_dimension("mg", "kg")
    assert not same_dimension("%", "mg")
    assert not same_dimension("M", "mg/mL")


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
    assert not same_dimension("log S", "pKa")
    assert not same_dimension("log S", "")
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
    for spelling in ("m", "cm", "mm", "um", "µm", "μm", "nm"):
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
