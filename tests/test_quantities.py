"""The number grammar, pinned to the tool output it exists to read.

Every fixture here is verbatim from a real tool result or a real live answer — the electronic
properties of p-chlorobenzenesulfonyl fluoride as `compute_electronic_properties` returns them, an
`ich_impurity_limit` PDE block, a `stoichiometry_table` row, a `gather_evidence` chunk envelope off
the wire, and the charge table gr-29 actually wrote. That is not decoration. The last idealized
fixture in this area hid that a retrieved-note envelope's quotes arrive JSON-escaped, so a pattern
that looked right matched nothing on a live run; and the emphasis case below (`**2000 g**`) was a
real draft of this module silently dropping five of the six masses in a charge table.
"""

from chemclaw.core.quantities import (
    is_rounding_of,
    labelled_values,
    returned_values,
    stated_numerals,
)

# `compute_electronic_properties(smiles="Clc1ccc(S(=O)(=O)F)cc1")`, verbatim, head of the result.
_PROPERTIES = (
    '{\n  "smiles": "O=S(=O)(F)c1ccc(Cl)cc1",\n'
    '  "structure_id": "st_8addd23b880dff9b",\n'
    '  "method": "GFN2-xTB",\n  "solvent": null,\n'
    '  "total_energy_hartree": -35.54362537022255,\n'
    '  "homo_ev": -11.827244634708782,\n'
    '  "lumo_ev": -7.947981771813835,\n'
    '  "gap_ev": 3.8792628628949473,\n'
    '  "dipole_debye": 4.557929224414533,\n'
)

# `ich_impurity_limit(substance="palladium")`, verbatim — the six values a live judge called
# invented while looking at a 200-character preview that stopped before the first of them.
_ICH_PALLADIUM = (
    '    "limits": [\n'
    '      {\n        "basis": "oral PDE",\n        "value": 100.0,\n'
    '        "unit": "µg/day"\n      },\n'
    '      {\n        "basis": "parenteral PDE",\n        "value": 10.0,\n'
    '        "unit": "µg/day"\n      },\n'
    '      {\n        "basis": "inhalation PDE",\n        "value": 1.0,\n'
    '        "unit": "µg/day"\n      }\n    ],\n'
)

# One `stoichiometry_table` row, verbatim: 1.2 equivalents of phenylboronic acid on a 2 kg basis.
_CHARGE_ROW = (
    '    {\n      "name": "OB(O)c1ccccc1",\n      "smiles": "OB(O)c1ccccc1",\n'
    '      "role": "reagent",\n      "equivalents": 1.2,\n'
    '      "molecular_weight": 121.93199999999996,\n'
    '      "moles_mmol": 12831.754314677388,\n'
    '      "mass_g": 1564.601467097243,\n'
    '      "density_g_per_ml": null,\n      "volume_ml": null\n    },\n'
)

# A `gather_evidence` chunk as it arrives on the wire, from a stored live transcript: the envelope
# is text inside a JSON string, so its quotes are escaped and its note id is a hyphenated slug.
_EVIDENCE = (
    '[{"content": "<retrieved-note-4ac8bd4b8ff10031 id=\\"reaction-liu-orgsyn-procedure-1\\">'
    '\\nReaction yield 40 percent"}]'
)


def test_a_correctly_rounded_quotation_is_recognised_as_the_value_it_came_from() -> None:
    """The crux: 4.56 D in an answer and 4.557929224414533 in the tool are the same number.

    A strict string match, or a prefix match on "4.55", would call every correctly-rounded figure
    in a live answer fabricated — which is the defect this module was written to remove, rebuilt.
    """
    values = returned_values(_PROPERTIES)
    assert is_rounding_of("4.56", values)
    assert is_rounding_of("-7.95", values)
    assert is_rounding_of("3.88", values)
    assert is_rounding_of("-11.83", values)


def test_a_figure_at_a_precision_the_value_does_not_support_is_not_a_quotation() -> None:
    """The precision the answer chose fixes the scale, in both directions.

    "4.5" is not a quotation of 4.5579 — that value rounds to 4.6 — and "4.55" is not either, even
    though it is a prefix of the digits. Without this the check would degenerate into "some tool
    returned something roughly like it", which grounds almost anything.
    """
    values = returned_values(_PROPERTIES)
    assert not is_rounding_of("4.5", values)
    assert not is_rounding_of("4.55", values)
    assert not is_rounding_of("-7.94", values)
    assert not is_rounding_of("4.5579293", values)  # right to seven places, wrong at the eighth


def test_rounding_is_half_up_the_way_a_person_rounds_not_half_even() -> None:
    """Python's `round` is banker's rounding, which would call a correctly-rounded figure invented.

    A model writing 4.55 from 4.545 is rounding the way a chemist does. Half-even gives 4.54, so
    the stated figure would match nothing and the answer would look like it had made it up.
    """
    assert is_rounding_of("4.55", [4.545])
    assert is_rounding_of("-4.55", [-4.545])


def test_the_six_ich_limits_a_live_judge_called_invented_are_all_recognised() -> None:
    """gr-26, end to end on the real result: the PDEs are quotations, not inventions.

    The judge's own words on the re-run were "the answer invents specific PDE numbers (Pd:
    100/10/1 µg/day)… the tool results shown are truncated previews that do not display the
    numerical limits". They were displayed here.
    """
    values = returned_values(_ICH_PALLADIUM)
    assert [is_rounding_of(figure, values) for figure in ("100", "10", "1")] == [True] * 3


def test_a_mass_is_recognised_through_the_markdown_the_answer_wrapped_it_in() -> None:
    """gr-29's charge table, verbatim on both sides: bold in a table cell is still a quantity.

    A draft of this pattern took the number only after a whitelisted boundary character, which did
    not include `*` — so five of the six masses in a real charge table were dropped, including
    every one the judge had called fabricated. Emphasis and pipes are formatting, not context.
    """
    answer = "| **Phenylboronic acid** | Coupling partner | **1565 g** | 1.2 equiv |"
    values = returned_values(_CHARGE_ROW)
    assert "1565" in stated_numerals(answer)
    assert is_rounding_of("1565", values)
    assert is_rounding_of("1.2", values)


def test_digits_inside_an_identifier_are_not_quantities() -> None:
    """A structure id, a SMILES and a note slug are names; reading them as numbers vouches falsely.

    Each of these is in the fixtures above, so the scan meets all three on every real result: a
    permissive pattern turns `st_8addd23b880dff9b` into four values and
    `reaction-liu-orgsyn-procedure-1` into a 1, and any of them could then ground a figure the
    tool never computed.
    """
    values = returned_values(_PROPERTIES)
    assert 8.0 not in values and 23.0 not in values and 880.0 not in values

    evidence = returned_values(_EVIDENCE)
    assert evidence == [40.0], "only the yield is a quantity; the id and the hash are names"


def test_a_code_span_or_a_wikilink_in_an_answer_is_not_a_quantity_claim() -> None:
    """An answer's SMILES and note citations carry digits and assert no figure whatsoever.

    Both are real from gr-18. The wikilink is scored as a citation by `_score_citations`; counting
    its trailing 0019 as a number too would have one id answer two different questions.
    """
    answer = (
        "**40% yield:** `Clc1ccc(S(=O)(=O)F)cc1` with DBU, see "
        "[[reaction-nielsen-deoxyfluorination-0019]] — dipole 4.56 D."
    )
    assert stated_numerals(answer) == ["40", "4.56"]


def test_a_hyphen_after_a_number_is_never_read_as_a_minus() -> None:
    """A table cell's "| -7.95 |" states −7.95; "10-20%" states no −20.

    Sign is what makes this a real distinction rather than a tidiness one: the list this feeds is
    quoted to a judge as "that figure is verbatim tool output", and a value whose sign the answer
    flipped is not that.

    The range's upper bound goes with the slug tails the same lookbehind exists to drop — pinned
    here as the price, not as an accident. Ranges are rare in these answers, hyphenated ids are on
    every line of every retrieval result, and losing a figure only costs it a verification.
    """
    assert stated_numerals("a 10-20% window") == ["10"]
    assert stated_numerals("| **LUMO** | -7.95 eV | -8.54 eV |") == ["-7.95", "-8.54"]


def test_returned_values_deduplicates_and_keeps_first_seen_order() -> None:
    """Same contract as `mentioned_ids`, and not cosmetic at the sizes this runs at.

    One 18-chunk `gather_evidence` sweep holds 133 numeric literals and 35 distinct values, and it
    is the distinct set the comparison needs and the event carries.
    """
    assert returned_values('{"a": 12.5, "b": 3, "c": 12.5, "d": 3}') == [12.5, 3.0]


# --- labelled values: the same numbers, under the names the tool gave them -----------------------


def test_a_json_result_names_every_number_it_returned() -> None:
    """The label is the payload's key path, verbatim — never prettified and never inferred.

    A surface printing `pka 4.76` and `sd 1.6` side by side is telling the truth; one printing
    `pKa 4.76 ± 1.6` is not, unless the tool said so. The names are the whole of what this adds,
    so they are the whole of what is asserted.
    """
    values = labelled_values('{"pka": 4.76, "sd": 1.6}')
    assert [(v.label, v.value, v.unit) for v in values] == [("pka", 4.76, ""), ("sd", 1.6, "")]


def test_a_unit_is_read_only_from_the_object_that_states_it() -> None:
    """`{"basis": …, "value": 0.5, "unit": "µg/day"}` — the shape the ICH tables use.

    Read from a parent or a sibling object it would be a guess about which numbers it applies to;
    inside a list under the key that states it, it is the same sentence written once.
    """
    limit = labelled_values(
        '{"limit": {"limits": [{"basis": "oral", "value": 0.5, "unit": "\u00b5g/day"}]}}'
    )
    assert [(v.label, v.unit) for v in limit] == [("limit.limits.0.value", "\u00b5g/day")]

    shared = labelled_values('{"unit": "eV", "homo": -11.8, "lumo": -7.9}')
    assert {v.unit for v in shared} == {"eV"}


def test_prose_is_left_to_the_bare_numbers() -> None:
    """A non-JSON result yields no labels at all, and that is the honest report.

    Falling back on the number grammar and pairing each value with whatever word preceded it would
    be exactly the invented relationship this refuses. The figures are still on the wire — they
    simply arrive unnamed, which is what they are.
    """
    assert labelled_values("the pKa is about 4.76") == []
    assert returned_values("the pKa is about 4.76") == [4.76]


def test_a_boolean_is_not_a_quantity() -> None:
    """`bool` is an `int` in Python, and "converged 1" is a number nobody computed."""
    assert [v.label for v in labelled_values('{"converged": true, "energy": -35.5}')] == ["energy"]


def test_an_unlabelled_number_belongs_to_the_other_reader() -> None:
    """A bare top-level list has no names to give, so it yields none rather than indices alone."""
    assert labelled_values("[1, 2, 3]") == []
    assert returned_values("[1, 2, 3]") == [1.0, 2.0, 3.0]


def test_the_same_label_and_value_is_recorded_once() -> None:
    """Deduplicated on the pair, not on the value.

    A repeated column heading does not repeat the entry, and two different keys holding one value
    stay two entries — the names are the point, so collapsing on the number alone would lose one.
    """
    values = labelled_values('{"rows": [{"mass_g": 2.0}, {"mass_g": 2.0}], "total_g": 2.0}')
    assert [(v.label, v.value) for v in values] == [
        ("rows.0.mass_g", 2.0),
        ("rows.1.mass_g", 2.0),
        ("total_g", 2.0),
    ]
