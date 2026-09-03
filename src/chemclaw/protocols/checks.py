"""The deterministic verdicts a drafted design has to survive.

**These are computed, never asserted.** Nothing here asks a model whether its own protocol is
sound; every function reads the design and answers from arithmetic, RDKit, or the request's own
stated limits. That is the whole reason this file exists rather than a paragraph in a `SKILL.md`:
a prompt can ask for a hazard screen and a prompt can be ignored, while
`evidence_present` returning a blocker stops the draft from being stored at all.

Each check is a pure `ExperimentDesign -> ProtocolCheck` and they are run as a set by `run_checks`,
so a new one is a function plus a row in `_CHECKS` and nothing else moves.

**Severity is per case, not per check**, which is the thing to read before adding one.
`charge_is_consistent` is a `blocker` when the table names no limiting reagent or its equivalents
contradict its amounts, and a `warning` when there is no table yet — the same function, two
severities, because the question "is this misleading" has different answers on its branches.

A `blocker` is for the cases where storing the design would be storing something misleading: a
structure nobody can read, a charge table nobody can weigh out, an arm setting a level the factor
does not declare, a plate that does not fit, a reagent the chemist forbade, no followable evidence
at all. A `warning` is a judgment about a specific piece of work that this file is not entitled to
make: a missing control, an unmeasured objective, an unscreened hazard, a temperature outside the
band a unit mistake leaves.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import Any

from chemclaw.core.chem import InvalidSmilesError, element_counts
from chemclaw.core.reagents import resolve_compound_name
from chemclaw.protocols.layout import PLATE_SHAPES, capacity, plate_shape, well_label
from chemclaw.protocols.models import (
    ChargeLine,
    CheckSeverity,
    CheckStage,
    EvidenceRef,
    ExperimentDesign,
    ProtocolCheck,
    Setpoints,
)
from chemclaw.science.labels.vocabulary import SpeciesRole

#: Temperature outside this band is almost always a unit mistake (a Kelvin value typed into a
#: Celsius field reads as 353 °C) rather than a real setpoint. A warning, not a blocker: sub-zero
#: cryogenic work and high-temperature flow chemistry both live outside a narrow band.
_TEMPERATURE_BAND_C = (-100.0, 300.0)

#: Above this, a "time" is almost certainly minutes typed into an hours field.
_MAX_PLAUSIBLE_HOURS = 336.0

#: The rest of the bands a unit mistake leaves, each on a field the check's docstring already
#: claimed to cover and none of which it read. Measured: a step at 5000 °C for a million hours, a
#: 1000 M concentration (millimolar typed into a molar field), 10^6 bar, pH -50, a tonne of solid,
#: a swimming pool of solvent and 500 equivalents all passed as "setpoints and amounts are in
#: range". The models accept every one of them, and correctly so — only inf and NaN are refused
#: there, because a bound belongs in a check a chemist can see and overrule, not in a parser.
_MAX_MOLAR = 100.0
_MAX_BAR = 1000.0
_PH_BAND = (-2.0, 16.0)
_MAX_EQUIVALENTS = 200.0
_MAX_MASS_MG = 1_000_000.0
_MAX_VOLUME_ML = 20_000.0


def _ok(check_id: str, severity: CheckSeverity, detail: str = "") -> ProtocolCheck:
    return ProtocolCheck(check_id=check_id, severity=severity, passed=True, detail=detail)


def _fail(check_id: str, severity: CheckSeverity, detail: str) -> ProtocolCheck:
    return ProtocolCheck(check_id=check_id, severity=severity, passed=False, detail=detail)


def _all_charge_lines(design: ExperimentDesign) -> list[ChargeLine]:
    """Every charge line in the design.

    A function rather than `design.base.charge` inline, because there is exactly one charge table
    today and four checks read it through this seam.

    **It is a seam, not a guarantee, and the docstring used to claim the second.** It said a
    per-arm override "lands here and none of them changes" — while `charge_is_consistent` (the
    blocker whose whole subject is the charge table) and `is_a_protocol` both read
    `design.base.charge` directly. Either could still be blind to an override this function
    started returning. The two are left as they are, because `charge_is_consistent` needs the
    per-arm question answered deliberately rather than inherited, and this sentence is now what a
    reader is told instead of a promise nothing kept.
    """
    return list(design.base.charge)


def _structures(design: ExperimentDesign) -> list[tuple[str, str]]:
    """Every `(where, smiles)` the design names, so one pass can check them all.

    **The reaction SMILES is deliberately NOT in here, and putting it in was a measured mistake.**
    It reads like the one structure a design always has, so it was added to close a hole in
    `components_resolve`. Two things broke at once. `forbidden_absent` reads this same set, and a
    precedent's record form carries the *old* solvent in its agent slot — so the canonical
    "get me out of DMF" design, whose own solvent is 2-MeTHF, was refused at intake with "the
    design uses reagents the request forbids: DMF", permanently and with no way to state both the
    precedent and the exclusion. And an agent block is routinely written as a *name* rather than a
    structure, so `CCO.CC(=O)Cl>DMF>CCOC(C)=O` became a blocker while `A>B>C>D` and
    `Suzuki coupling` passed, because the gate was triggered by counting `>`.

    The distinction the hole and the fix both missed: `reaction_smiles` says what is being *asked
    for*; this set says what the design *does*. `atom_balance` reads the reaction on its own terms
    and reports an unreadable species as a failed warning, which is the right severity for a field
    that is free text by declaration (`models.py`: "Empty for an ask that is not one").
    """
    asked = [
        (f"request component {component.name_as_written!r}", component.smiles)
        for component in design.request.components
        if component.smiles
    ]
    return [*asked, *_used_structures(design)]


def _used_structures(design: ExperimentDesign) -> list[tuple[str, str]]:
    """Every `(where, smiles)` the design *does*, as opposed to the ones the ask names.

    The same distinction `_structures` draws for `reaction_smiles`, one field further in — and
    missing it left the identical defect in place. A `RequestedComponent` is "one species the
    chemist **named**", and what a process chemist names first is the incumbent they want replaced,
    so the canonical "get me out of DMF" design — solvent `2-MeTHF`, `forbidden=["DMF"]`, DMF listed
    as the named component — was refused by `forbidden_absent` with "the design uses reagents the
    request forbids: DMF". `draft_experiment_protocol` raises on any blocker, so that design could
    never be stored and there was no way to state both the incumbent and the exclusion.

    `components_resolve` still reads the ask, because whether a name the chemist typed resolves is
    exactly its question.
    """
    found: list[tuple[str, str]] = []
    for line in _all_charge_lines(design):
        if line.smiles:
            found.append((f"charge line {line.component!r}", line.smiles))
    for factor in design.factors:
        for level in factor.levels:
            if level.smiles:
                found.append((f"factor {factor.name}/{level.label}", level.smiles))
    return found


def components_resolve(design: ExperimentDesign) -> ProtocolCheck:
    """Every structure the design names parses whole."""
    # **`_parses` alone, and the `canonical == smiles` conjunction this replaced is the defect
    # worth remembering.** `canonical_smiles` is lenient in a way that is not "returns the input
    # unchanged": RDKit stops at whitespace and at a non-ASCII edge, so `"CCO junk"` canonicalises
    # to `"CCO"` — a *different, smaller molecule*, successfully. The old test asked whether the
    # string came back unchanged, which is false for exactly that class, so the blocker never
    # consulted the strict parser on the inputs it was written for and reported `'1 structures
    # parse'` about a structure that does not. Measured: `"CCO junk"`, `"CCO 1"`, `"CC°"` and
    # `"°C"` all passed. It is the same silent truncation `require_molecule`'s docstring records
    # for `screen_hazards("CCO junk")`, one layer up.
    #
    # Asking `require_molecule` directly instead cannot fail open, and measurement says it does not
    # fail *closed* either: the only inputs where the lenient and strict parsers disagree in the
    # other direction are over `molecule_max_atoms`/`molecule_max_smiles_length`, which both
    # already reject.
    bad = [where + f": {smiles!r}" for where, smiles in _structures(design) if not _parses(smiles)]
    if bad:
        return _fail(
            "components_resolve",
            "blocker",
            "these structures do not parse: " + "; ".join(bad),
        )
    named_without_structure = [c.name_as_written for c in design.request.components if not c.smiles]
    if named_without_structure:
        # A *failed* warning: this is a finding, and `render_markdown` and `summarise` both list
        # only failed checks, so an `_ok` here put "checked and fine" in front of a reader about a
        # species nobody resolved. That is the defect `_unreadable`'s docstring describes as fixed,
        # in four other places in this file.
        return _fail(
            "components_resolve",
            "warning",
            "no structure resolved for: " + ", ".join(named_without_structure),
        )
    return _ok("components_resolve", "blocker", f"{len(_structures(design))} structures parse")


def _parses(smiles: str) -> bool:
    try:
        element_counts(smiles)
    except InvalidSmilesError:
        return False
    return True


def _agreement_tolerance(stated_mmol: float) -> float:
    """How far a stated amount may sit from the one its equivalents imply.

    **A flat 2% of the line's own figure blocked ordinarily-rounded catalyst lines.** The tolerance
    scaled with the line, so for anything under about 0.25 mmol a two-decimal entry could not clear
    it: 250 mg of a 182 g/mol aryl halide is 1.37 mmol, 5 mol% Pd is 0.0685 mmol, a chemist writes
    `0.07`, and that was a *blocker* — which refuses the draft outright. Swept across the usual
    scales and loadings, 4 of 18 correct tables were refused, every one of them a normal catalyst or
    ligand line at a non-round limiting scale.

    The fix is to allow the rounding that is actually written and nothing more. `Decimal(repr(…))`
    recovers the precision of the figure as it was typed (`0.07` → two decimals → half a unit in the
    last place is 0.005), `normalize` keeps a whole number whole through JSON's `68` → `68.0`, and
    the exponent is clamped at 0 so a round `100` buys half a unit rather than fifty. The 2% term
    stays for the large lines, where the written precision is coarser than the real agreement.

    A gross error still fails: a catalyst written `0.10` against an implied `0.0685` is 0.0315 out,
    six times the slack, and a factor-of-ten slip is never close.
    """
    exponent = min(int(Decimal(repr(stated_mmol)).normalize().as_tuple().exponent), 0)
    rounding_slack = 0.5 * 10.0**exponent
    return max(0.02 * abs(stated_mmol), rounding_slack)


def charge_is_consistent(design: ExperimentDesign) -> ProtocolCheck:
    """The charge table names exactly one limiting reagent and its equivalents agree with it."""
    lines = design.base.charge
    if not lines:
        return _ok("charge_is_consistent", "warning", "no charge table")
    limiting = [line for line in lines if line.limiting]
    if len(limiting) != 1:
        return _fail(
            "charge_is_consistent",
            "blocker",
            f"{len(limiting)} charge lines are marked limiting; exactly one has to be, because "
            "every equivalents figure is relative to it",
        )
    reference = limiting[0]
    if reference.equivalents is not None and abs(reference.equivalents - 1.0) > 1e-6:
        return _fail(
            "charge_is_consistent",
            "blocker",
            f"the limiting reagent {reference.component!r} is listed at "
            f"{reference.equivalents} equivalents; by definition it is 1.0",
        )
    # `not` rather than `is None`, and that is the fix for a measured hole: `amount_mmol` is
    # `ge=0.0`, so a limiting reagent at exactly `0.0` was neither `None` nor caught below — the
    # `reference.amount_mmol > 0` guard inside the comprehension emptied the disagreement list, and
    # a table where every mmol figure contradicted its equivalents returned a *passing* blocker
    # reading "limiting reagent 'SM' at 0 mmol". Zero is the same fact as absent for this check:
    # there is no scale to turn an equivalent into a weight against.
    if not reference.amount_mmol:
        return _fail(
            "charge_is_consistent",
            "warning",
            f"the limiting reagent {reference.component!r} has no usable amount "
            f"({reference.amount_mmol!r}), so no other line's equivalents can be turned into a "
            "weight",
        )
    # Equivalents and amounts are two statements of the same fact, and a table where they disagree
    # is one a chemist will weigh out wrong. The tolerance is 2% of the line's own figure **or the
    # rounding a chemist actually wrote, whichever is larger** — see `_agreement_tolerance`.
    disagreements = [
        f"{line.component!r}: {line.equivalents} eq implies "
        f"{line.equivalents * reference.amount_mmol:.4g} mmol, table says {line.amount_mmol:.4g}"
        for line in lines
        if line.equivalents is not None
        and line.amount_mmol is not None
        and abs(line.equivalents * reference.amount_mmol - line.amount_mmol)
        > _agreement_tolerance(line.amount_mmol)
    ]
    if disagreements:
        return _fail(
            "charge_is_consistent",
            "blocker",
            "equivalents and amounts disagree by more than 2%: " + "; ".join(disagreements),
        )
    return _ok(
        "charge_is_consistent",
        "blocker",
        f"limiting reagent {reference.component!r} at {reference.amount_mmol:.4g} mmol",
    )


def limiting_is_limiting(design: ExperimentDesign) -> ProtocolCheck:
    """The line marked limiting is the one that actually runs out first.

    `charge_is_consistent` enforces that the reference sits at 1.0 equivalents and that every other
    line's amount agrees with its equivalents. Neither says the reference is the *minimum*, so a
    perfectly self-consistent table can name the wrong one: acid at 1.0 eq / 1.0 mmol marked
    limiting beside an amine at 0.5 eq / 0.5 mmol passed with no blockers, while the amine caps the
    reaction at 0.5 mmol. The run sheet then names the wrong reference and every yield stated
    against it is over-reported twofold.

    A `warning` rather than a blocker, and the severity is the argument: the roles are declared by
    whoever wrote the table, and a deliberately sub-stoichiometric reagent whose yield is reported
    on the other partner is unusual rather than impossible. That is precisely this file's own
    definition of a warning — a judgment about a specific piece of work it is not entitled to make —
    and `forbidden_absent` is the standing reminder of what a blocker firing on correct work costs.

    Only `starting-material` and `reagent` lines are weighed. A catalyst, a ligand, an additive, a
    base or a solvent below one equivalent is the normal case, not a finding.
    """
    lines = design.base.charge
    limiting = [line for line in lines if line.limiting]
    # The walrus binds the reference amount as a `float` for the comparison below: narrowing on
    # `limiting[0].amount_mmol` narrows the expression rather than the attribute.
    if len(limiting) != 1 or not (reference_mmol := limiting[0].amount_mmol or 0.0):
        # `charge_is_consistent` already reports both of these, and one fault should be one finding.
        return _ok("limiting_is_limiting", "warning", "no single limiting line to weigh")
    reference = limiting[0]
    stoichiometric = {SpeciesRole.STARTING_MATERIAL, SpeciesRole.REAGENT}
    smaller = [
        f"{line.component!r} at {line.amount_mmol:.4g} mmol"
        for line in lines
        if line is not reference
        and line.role in stoichiometric
        and line.amount_mmol is not None
        and line.amount_mmol < reference_mmol
    ]
    if smaller:
        return _fail(
            "limiting_is_limiting",
            "warning",
            f"{reference.component!r} is marked limiting at {reference_mmol:.4g} mmol, but "
            + ", ".join(smaller)
            + " runs out first. Every equivalents figure and any yield are stated against the "
            "limiting reagent, so mark the one that actually caps the reaction.",
        )
    return _ok(
        "limiting_is_limiting",
        "warning",
        f"{reference.component!r} is the smallest stoichiometric charge",
    )


def atom_balance(design: ExperimentDesign) -> ProtocolCheck:
    """No expected product contains an element nothing charged supplies."""
    # The same rule `ingest.eln.validate.validate_ord` applies to a recorded reaction, asked of a
    # proposed one. Counts are deliberately not compared, for that function's reason: there are no
    # stoichiometric coefficients here either, so a dimerization would fail a per-molecule count.
    # **Both forms, because this tree emits both.** `ingest.eln.ord.reaction_smiles()` produces the
    # record form `reactants>agents>products`, so an agent copying a precedent's reaction across
    # brings a three-part string whenever that run had a solvent or a catalyst — and a `">>" not
    # in reaction` guard skipped the check silently on exactly those. Splitting on `>` handles both:
    # `a>>c` is three parts with an empty middle, `a>b>c` is three parts with agents in it. Agents
    # supply elements (they are in the flask), so they join the input side.
    reaction = design.request.reaction_smiles.strip()
    parts = reaction.split(">")
    if len(parts) != 3:
        if not reaction:
            return _ok("atom_balance", "warning", "no reaction SMILES to balance")
        return _fail("atom_balance", "warning", f"not a reaction: {reaction!r}")
    reactant_side, agent_side, product_side = parts
    supplied: set[str] = set()
    inputs = (
        _split_species(reactant_side)
        + _split_species(agent_side)
        + [line.smiles for line in _all_charge_lines(design)]
    )
    for smiles in inputs:
        if not smiles:
            continue
        try:
            supplied.update(element_counts(smiles))
        except InvalidSmilesError:
            return _unreadable(smiles)
    missing: set[str] = set()
    for smiles in _split_species(product_side):
        try:
            missing.update(set(element_counts(smiles)) - supplied)
        except InvalidSmilesError:
            return _unreadable(smiles)
    if missing:
        return _fail(
            "atom_balance",
            "warning",
            "the product contains elements nothing charged supplies: "
            + ", ".join(sorted(missing))
            + " — either a species is missing from the charge table or the product is wrong",
        )
    return _ok("atom_balance", "warning", "every product element is supplied")


def _unreadable(smiles: str) -> ProtocolCheck:
    """The verdict when a species in the reaction cannot be read.

    A **failed** warning rather than a passing one, which is the second half of the same defect as
    `components_resolve`'s: returning `_ok` here put "checked and fine" in front of a reader about a
    balance nobody could compute, and `render_markdown` lists only failed checks — so the sentence
    naming the unreadable species never reached the page. The severity stays a warning, because a
    structure this check cannot read is `components_resolve`'s blocker to raise, not this one's.
    """
    return _fail("atom_balance", "warning", f"could not read {smiles!r}; balance not checked")


def _split_species(side: str) -> list[str]:
    """The species in one block of a reaction SMILES."""
    return [part for part in side.split(".") if part]


def factor_levels_declared(design: ExperimentDesign) -> ProtocolCheck:
    """Every arm sets levels its factors declare, and sets all of them."""
    declared = {f.name: {level.label for level in f.levels} for f in design.factors}
    if not design.factors:
        # **Not an early return, because "no factors" is the case where every level an arm sets is
        # undeclared.** The exit came first and exempted exactly that: two arms setting `solvent`
        # and `ligand` with no factor declared passed a blocker whose subject is "an arm setting a
        # level the factor does not declare", and `render`'s run sheet — which builds its columns
        # from `design.factors` — then dropped those values from the sheet the chemist runs from.
        stray = sorted({name for arm in design.arms for name in arm.levels if not arm.control})
        if stray:
            return _fail(
                "factor_levels_declared",
                "blocker",
                "arms set levels for factors this design does not declare: "
                + ", ".join(stray)
                + " — declare them as factors, or the run sheet will not show them",
            )
        return _ok("factor_levels_declared", "blocker", "no factors")
    problems: list[str] = []
    for arm in design.arms:
        if arm.control:
            continue  # A control is deliberately outside the factor space.
        unknown = sorted(set(arm.levels) - set(declared))
        if unknown:
            problems.append(f"{arm.arm_id} sets undeclared factor(s): {', '.join(unknown)}")
        for name, label in arm.levels.items():
            if name in declared and label not in declared[name]:
                problems.append(
                    f"{arm.arm_id} sets {name}={label!r}, which is not a declared level"
                )
        unset = sorted(set(declared) - set(arm.levels))
        if unset:
            problems.append(f"{arm.arm_id} does not set: {', '.join(unset)}")
    if problems:
        return _fail("factor_levels_declared", "blocker", "; ".join(problems))
    return _ok(
        "factor_levels_declared",
        "blocker",
        f"{len(design.arms)} arms over {len(design.factors)} factors",
    )


def arms_are_distinct(design: ExperimentDesign) -> ProtocolCheck:
    """No two non-replicate arms set the same conditions."""
    # **The setpoints are part of the conditions, and leaving them out made the remedy impossible.**
    # Keyed on `levels` alone, three arms differing only in temperature collided, and the message
    # told the chemist to mark one `replicate_of` the other — which the model validator refuses,
    # because they run different conditions. The advice and the refusal were each right about a
    # different definition of "the same conditions"; this is the one both now use.
    seen: dict[tuple[Any, ...], str] = {}
    duplicates: list[str] = []
    for arm in design.arms:
        if arm.replicate_of or arm.control:
            continue
        key = (tuple(sorted(arm.levels.items())), design.setpoints_for(arm))
        if key in seen:
            duplicates.append(f"{arm.arm_id} repeats {seen[key]}")
        else:
            seen[key] = arm.arm_id
    if duplicates:
        return _fail(
            "arms_are_distinct",
            "warning",
            "; ".join(duplicates)
            + " — mark an intended repeat with `replicate_of` so it is read as a replicate rather "
            "than as a duplicated row",
        )
    return _ok("arms_are_distinct", "warning", "no unmarked duplicate conditions")


def layout_fits(design: ExperimentDesign) -> ProtocolCheck:
    """The plate holds every arm, once, in a known format."""
    layout = design.layout
    if layout is None:
        # **The design's own shape, not `request.mode`.** `mode` is a field of the *ask*, and
        # nothing ties it to what was actually drafted, so a 96-arm design whose ask still said
        # `single` switched this blocker off entirely and reported "a single experiment needs no
        # layout" over ninety-six arms. One mis-set enum on the intake disabled the plate-fits
        # blocker and the controls warning on a real plate. `summarise` had this same defect and
        # was fixed the same way: read the design.
        if not design.is_plate:
            return _ok("layout_fits", "blocker", "a single experiment needs no layout")
        return _ok("layout_fits", "warning", "no plate layout")
    if layout.plate_format not in PLATE_SHAPES:
        return _fail("layout_fits", "blocker", f"unknown plate format {layout.plate_format}")
    if len(layout.wells) > capacity(layout.plate_format):
        return _fail(
            "layout_fits",
            "blocker",
            f"{len(layout.wells)} wells on a {layout.plate_format}-well plate",
        )
    labels = [w.label for w in layout.wells]
    if len(set(labels)) != len(labels):
        return _fail("layout_fits", "blocker", "two arms are placed in the same well")
    # **Counted, not set-compared, because "once" is half of what this blocker claims.** The
    # docstring says the plate holds every arm *once*; a set could only ever see an arm that is
    # missing, never one placed twice. Measured: three wells over two arms with A1 in two of them
    # passed as `3 of 96 wells used`, and `run_sheet_rows` — which keys wells by arm — then dropped
    # a well, so the chemist's run sheet started at run 2 and put A1 at the wrong position.
    # `Counter` rather than `list.count` in a comprehension — the O(n²) shape `diff._labelled`
    # carried, kept out of a second place for the same reason and with the same honest weight: at
    # this list's ceiling (1536 wells) that scan is 22 ms, not the 46 s measured before the
    # ceilings existed.
    occupants = Counter(w.arm_id for w in layout.wells)
    twice = sorted(arm for arm, n in occupants.items() if n > 1)
    if twice:
        return _fail(
            "layout_fits",
            "blocker",
            "these arms are placed in more than one well: " + ", ".join(twice),
        )
    placed = set(occupants)
    arm_ids = {a.arm_id for a in design.arms}
    if placed != arm_ids:
        unplaced = sorted(arm_ids - placed)
        stray = sorted(placed - arm_ids)
        parts = []
        if unplaced:
            parts.append("arms with no well: " + ", ".join(unplaced))
        if stray:
            parts.append("wells naming no arm: " + ", ".join(stray))
        return _fail("layout_fits", "blocker", "; ".join(parts))
    orders = sorted(w.run_order for w in layout.wells)
    if orders != list(range(1, len(orders) + 1)):
        return _fail("layout_fits", "blocker", "run order is not 1..n over the wells")
    # **The plate's own shape and each well's position, because only `place()` is trusted.**
    # `POST /protocols/{id}/revisions` accepts a whole `PlateLayout` from a browser, so nothing
    # guarantees the layout came from `place()` at all. Measured before this: a layout declaring
    # `plate_format=96` with `rows=1, columns=2` and wells at row 98 labelled `ZZ99` passed a
    # *blocker* whose docstring says "the plate holds every arm, once, in a known format".
    rows, columns = plate_shape(layout.plate_format)
    if (layout.rows, layout.columns) != (rows, columns):
        return _fail(
            "layout_fits",
            "blocker",
            f"a {layout.plate_format}-well plate is {rows}x{columns}, and this layout declares "
            f"{layout.rows}x{layout.columns}",
        )
    off_plate = [
        f"{w.label} at row {w.row}, column {w.column}"
        for w in layout.wells
        if not (0 <= w.row < rows and 0 <= w.column < columns)
    ]
    if off_plate:
        return _fail(
            "layout_fits", "blocker", "these wells are not on the plate: " + "; ".join(off_plate)
        )
    mislabelled = [
        f"{w.label} is row {w.row}, column {w.column}, which is {well_label(w.row, w.column)}"
        for w in layout.wells
        if w.label != well_label(w.row, w.column)
    ]
    if mislabelled:
        return _fail("layout_fits", "blocker", "; ".join(mislabelled))
    return _ok(
        "layout_fits",
        "blocker",
        f"{len(layout.wells)} of {capacity(layout.plate_format)} wells used",
    )


def controls_present(design: ExperimentDesign) -> ProtocolCheck:
    """A plate carries at least one control."""
    # The design's shape rather than the ask's `mode`, for the reason `layout_fits` gives.
    if not design.is_plate:
        return _ok("controls_present", "warning", "not a plate")
    controls = [arm.arm_id for arm in design.arms if arm.control]
    if not controls:
        return _fail(
            "controls_present",
            "warning",
            "no control on the plate — a screen with nothing to compare against cannot tell a "
            "flat result from a failed run",
        )
    return _ok("controls_present", "warning", "controls: " + ", ".join(controls))


def evidence_present(design: ExperimentDesign) -> ProtocolCheck:
    """The design cites at least one precedent and at least one tool."""
    # The blocker that makes "use the record and the tools" a property of the code. A protocol
    # citing neither is a guess, and storing it would put a guess in the same table, with the same
    # shape and the same UI treatment, as one argued from 40 runs and a hazard screen.
    # **A citation counts only when it is followable**, which is the difference between this check
    # and a word count. `kind="tool"` without a `tool` name and `kind="precedent"` without a `ref`
    # are two sentences a model can write about work it did not do — measured, they cleared this
    # blocker between them — and neither gives a chemist anything to open. `hazard_screen_ran` was
    # already written this way (it reads `ref.tool` against three named tools); this is the same
    # rule one level up.
    cited = {ref.kind for ref in design.evidence if _is_followable(ref)}
    unfollowable = [ref.summary for ref in design.evidence if not _is_followable(ref)]
    kinds = cited
    grounded = kinds & {"precedent", "record", "note", "observation"}
    if not grounded and "tool" not in kinds:
        return _fail(
            "evidence_present",
            "blocker",
            (
                # **The message has to say which of the two failures this is.** Citations that
                # exist but are unfollowable produce the same empty `kinds` as no citations at all,
                # and this branch said "this design cites nothing" over two supplied references —
                # sending the model back to re-run five search tools when the fix was one empty
                # field. The passing branch already knew how to describe it.
                f"{len(unfollowable)} citations are supplied but none is followable: "
                + "; ".join(unfollowable[:3])
                + ". A `precedent` needs its `ref` and a `tool` needs its `tool` name — without "
                "them a chemist has nothing to open"
                if unfollowable
                else "this design cites nothing. Search the record (substrate_precedent, "
                "conditions_for_similar_reaction, reagent_frequency, similar_reactions, "
                "gather_evidence) and compute what it does not state, then cite what you used in "
                "`evidence`"
            ),
        )
    if not grounded:
        return _fail(
            "evidence_present",
            "warning",
            "no precedent cited — the conditions rest entirely on computed or predicted values, "
            "which is a real answer only when the record genuinely holds nothing comparable. Say "
            "so to the chemist",
        )
    if "tool" not in kinds:
        return _fail(
            "evidence_present",
            "warning",
            "precedent is cited but nothing was computed. Anything the record does not state — a "
            "pKa, a solubility, a solvent ranking, a hazard — is a tool call, not an assumption",
        )
    counted = len(design.evidence) - len(unfollowable)
    detail = f"{counted} citations across {', '.join(sorted(kinds))}"
    if unfollowable:
        detail += "; not counted, because nothing names what to open: " + "; ".join(
            unfollowable[:3]
        )
    return _ok("evidence_present", "blocker", detail)


def _is_followable(ref: EvidenceRef) -> bool:
    """A citation a reader can act on: a tool call names its tool, everything else names its ref."""
    return bool(ref.tool.strip()) if ref.kind == "tool" else bool(ref.ref.strip())


def hazard_screen_ran(design: ExperimentDesign) -> ProtocolCheck:
    """A structural hazard screen was run over the design's species."""
    screens = {"screen_hazards", "screen_genotoxic_alerts", "ich_impurity_limit"}
    ran = sorted({ref.tool for ref in design.evidence if ref.tool in screens})
    if not ran:
        return _fail(
            "hazard_screen_ran",
            "warning",
            "no hazard screen is cited. This system flags rather than certifies, so a screen is "
            "not a clearance — but an unscreened design does not even carry the flag",
        )
    return _ok("hazard_screen_ran", "warning", "screened by: " + ", ".join(ran))


def objectives_are_measured(design: ExperimentDesign) -> ProtocolCheck:
    """Every objective the request names has an analytic that measures it."""
    objectives = {o.strip().lower() for o in design.request.objectives if o.strip()}
    if not objectives:
        return _ok("objectives_are_measured", "warning", "no objective stated")
    measured = {m.strip().lower() for a in design.base.analytics for m in a.measures}
    unmeasured = sorted(objectives - measured)
    if unmeasured:
        return _fail(
            "objectives_are_measured",
            "warning",
            "nothing measures: "
            + ", ".join(unmeasured)
            + " — a plate whose objective no analytic reports comes back unanswerable",
        )
    return _ok("objectives_are_measured", "warning", f"{len(objectives)} objectives measured")


def quantities_are_plausible(design: ExperimentDesign) -> ProtocolCheck:
    """Setpoints and amounts are inside the range a unit mistake would leave."""
    problems: list[str] = []
    low, high = _TEMPERATURE_BAND_C
    for label, points in _all_setpoints(design):
        if points.temperature_c is not None and not low <= points.temperature_c <= high:
            problems.append(
                f"{label}: {points.temperature_c} °C is outside {low}..{high} — a Kelvin value in "
                "a Celsius field looks exactly like this"
            )
        if points.time_h is not None and points.time_h > _MAX_PLAUSIBLE_HOURS:
            problems.append(f"{label}: {points.time_h} h is over {_MAX_PLAUSIBLE_HOURS:.0f} h")
        if points.concentration_molar is not None and points.concentration_molar > _MAX_MOLAR:
            problems.append(
                f"{label}: {points.concentration_molar} M is over {_MAX_MOLAR:.0f} M — a "
                "millimolar figure in a molar field looks exactly like this"
            )
        if points.pressure_bar is not None and points.pressure_bar > _MAX_BAR:
            problems.append(f"{label}: {points.pressure_bar} bar is over {_MAX_BAR:.0f} bar")
        if points.ph is not None and not _PH_BAND[0] <= points.ph <= _PH_BAND[1]:
            problems.append(f"{label}: pH {points.ph} is outside {_PH_BAND[0]}..{_PH_BAND[1]}")
    # **A step's own temperature and duration, which nothing read.** The docstring said "setpoints
    # and amounts", and a step reading "heat to 5000 °C for 1 000 000 h" passed as "setpoints and
    # amounts are in range" — a step's temperature is the number a chemist actually sets the block
    # to, so it is exactly as much a setpoint as the body's.
    for index, step in enumerate(design.base.steps, start=1):
        if step.temperature_c is not None and not low <= step.temperature_c <= high:
            problems.append(f"step {index}: {step.temperature_c} °C is outside {low}..{high}")
        if step.duration_h is not None and step.duration_h > _MAX_PLAUSIBLE_HOURS:
            problems.append(
                f"step {index}: {step.duration_h} h is over {_MAX_PLAUSIBLE_HOURS:.0f} h"
            )
    for line in _all_charge_lines(design):
        if line.equivalents == 0.0 and not line.limiting:
            problems.append(f"charge line {line.component!r} is 0 equivalents")
        if line.equivalents is not None and line.equivalents > _MAX_EQUIVALENTS:
            problems.append(
                f"charge line {line.component!r} at {line.equivalents} equivalents is over "
                f"{_MAX_EQUIVALENTS:.0f} — a solvent is charged by volume, not by equivalents"
            )
        if line.mass_mg is not None and line.mass_mg > _MAX_MASS_MG:
            problems.append(f"charge line {line.component!r}: {line.mass_mg} mg is over 1 kg")
        if line.volume_ml is not None and line.volume_ml > _MAX_VOLUME_ML:
            problems.append(f"charge line {line.component!r}: {line.volume_ml} mL is over 20 L")
    if problems:
        return _fail("quantities_are_plausible", "warning", "; ".join(problems))
    return _ok("quantities_are_plausible", "warning", "setpoints and amounts are in range")


def _all_setpoints(design: ExperimentDesign) -> Iterable[tuple[str, Setpoints]]:
    yield "base", design.base.setpoints
    for arm in design.arms:
        if arm.setpoints is not None:
            yield f"arm {arm.arm_id}", arm.setpoints


def forbidden_absent(design: ExperimentDesign) -> ProtocolCheck:
    """Nothing the chemist forbade appears in the design."""
    forbidden = [f.strip() for f in design.request.forbidden if f.strip()]
    if not forbidden:
        return _ok("forbidden_absent", "blocker", "nothing forbidden")
    # **Both sides go through the reagent table, and that is the fix rather than a refinement.**
    # The first version compared `canonical_smiles(term)` against the design's canonical SMILES —
    # but `canonical_smiles("DMF")` is the string `"DMF"`, because RDKit cannot read a name, so the
    # structure half could never fire for a reagent a chemist named. It worked only when the
    # exclusion was itself written as a SMILES, which is not how anybody writes an exclusion.
    # Measured: forbidding "DMF" let a design charging `N,N-dimethylformamide` — the same molecule,
    # same structure — through a *blocker*.
    #
    # `core.reagents.resolve_compound_name` is the one entry point this tree already has for
    # "give me the canonical form of whatever was typed", name or SMILES, and it returns `None`
    # rather than guessing. So both the exclusion and every species the design names are reduced to
    # a structure where one is known, and the written names are still compared beside it for the
    # reagents the table does not carry.
    names = {n.strip().lower() for n in _used_species(design) if n.strip()}
    structures = {_identity(value) for value in (*names, *(s for _, s in _used_structures(design)))}
    hits = [
        term for term in forbidden if term.strip().lower() in names or _identity(term) in structures
    ]
    if hits:
        return _fail(
            "forbidden_absent",
            "blocker",
            "the design uses reagents the request forbids: " + ", ".join(hits),
        )
    return _ok("forbidden_absent", "blocker", f"{len(forbidden)} exclusions honoured")


def _identity(value: str) -> str:
    """The canonical structure behind a name or a SMILES, or the lower-cased text when neither.

    The falling-back branch is what keeps this usable for the reagents the curated table does not
    carry — a site's internal code name, a fragment nobody has drawn — where the written spelling
    is the only identity there is. `resolve_compound_name` never guesses, so an unrecognised name
    reaching this branch is a miss rather than a fabricated structure.
    """
    resolved = resolve_compound_name(value.strip())
    return resolved.smiles if resolved is not None else value.strip().lower()


def _used_species(design: ExperimentDesign) -> list[str]:
    """Every human-readable species name the design *uses*.

    Deliberately not the ask's own `components`: see `_used_structures` for the measured failure
    that inclusion caused. What a chemist names in the ask is frequently the thing they are trying
    to get rid of.

    **The solvent is in here, and its absence made the blocker unable to catch the commonest
    exclusion there is.** A process chemist's hard exclusion is nearly always a solvent — an ICH
    class-2 solvent, or one the plant cannot handle — and `Setpoints.solvent` was the one field
    this function did not read. Measured: a design forbidding DMF and *running in DMF* reported
    `1 exclusions honoured`, while the rendered protocol printed `- **Solvent:** DMF`. The per-arm
    override is the same hole one level down.

    A step's `components` are here for the same reason: a procedure reading "charge SM and DMF"
    names a reagent, whether or not the charge table lists it.
    """
    names = [line.component for line in _all_charge_lines(design)]
    names += [level.label for factor in design.factors for level in factor.levels]
    names += [points.solvent for _, points in _all_setpoints(design)]
    names += [points.atmosphere for _, points in _all_setpoints(design)]
    names += [component for step in design.base.steps for component in step.components]
    return names


def coverage_is_stated(design: ExperimentDesign) -> ProtocolCheck:
    """A screen either covers its factor grid or says how much of it it covers."""
    # **`campaign` is the exemption; `single` is not.** A campaign is allowed to ship a first round
    # that does not cover its factor space — that is what the enum distinguishes screen from
    # campaign *for*. Reading `== "screen"` made every other mode an exemption too, so a design with
    # factors and ninety-six arms whose ask still said `single` reported "not a fixed screen". The
    # exemption is now named rather than inferred, and the shape decides the rest.
    if design.request.mode == "campaign" or not design.factors:
        return _ok("coverage_is_stated", "note", "not a fixed screen")
    full = 1
    for factor in design.factors:
        full *= len(factor.levels)
    # **Distinct level combinations, not arms.** Counting arms compared a number to a product of
    # level counts, so four arms covering two of four combinations reported "full grid: 4 of 4
    # combinations" — and `render_markdown` prints that sentence to the chemist. A declared level
    # that is never run is exactly what this check exists to make somebody say out loud, and on
    # that input it stated the opposite, as a passing note nobody questions. An arm that leaves a
    # factor unset covers no combination of the grid, so it is not counted as one.
    names = [factor.name for factor in design.factors]
    covered = {
        tuple(arm.levels.get(name, "") for name in names)
        for arm in design.arms
        if not arm.control and not arm.replicate_of
    }
    real = len([combination for combination in covered if all(combination)])
    if real >= full:
        return _ok("coverage_is_stated", "note", f"full grid: {real} of {full} combinations")
    # **Passing, and the sentence reaches the page anyway** — `render_markdown` now lists every
    # `note`, not only failed checks. Flipping this to `_fail` was the wrong half of the fix: a
    # fractional factorial is a deliberate, textbook design and `generate_screening_design` emits
    # them, so every correct reduced plate reported a failed check it could not clear — the check
    # reads only factors and arms, and nothing in `ExperimentDesign` records the confounding
    # statement it asks for. That is this file's own `_REQUEST_STAGE` warning, one severity down:
    # a check that fires on the normal path is one a reader learns to ignore.
    return _ok(
        "coverage_is_stated",
        "note",
        f"reduced design: {real} of {full} combinations. Say which combinations were given up and "
        "which effects are therefore confounded — a fractional design presented as the whole "
        "screen is how a plate gets over-read",
    )


def is_a_protocol(design: ExperimentDesign) -> ProtocolCheck:
    """The design says what to do — it has at least one arm, one step or one charge line."""
    if design.has_protocol:
        return _ok(
            "is_a_protocol",
            "blocker",
            f"{len(design.arms)} arm(s), {len(design.base.steps)} step(s), "
            f"{len(design.base.charge)} charge line(s)",
        )
    return _fail(
        "is_a_protocol",
        "blocker",
        "this design has no arms, no steps and no charge table — it is a structured ask rather "
        "than a protocol. Draft the procedure before storing it as one",
    )


#: The checks, in the order a reader wants them. Order is deliberate: what is unreadable, then what
#: is arithmetically wrong, then what is missing, then what is merely worth knowing.
_CHECKS: tuple[Callable[[ExperimentDesign], ProtocolCheck], ...] = (
    is_a_protocol,
    components_resolve,
    charge_is_consistent,
    limiting_is_limiting,
    atom_balance,
    factor_levels_declared,
    arms_are_distinct,
    layout_fits,
    forbidden_absent,
    evidence_present,
    hazard_screen_ran,
    controls_present,
    objectives_are_measured,
    quantities_are_plausible,
    coverage_is_stated,
)

#: The checks that mean anything about a design holding only the structured ask. Everything else is
#: a question about a procedure that does not exist yet.
#:
#: **Two stages rather than one, because the first version reported a blocker a request could not
#: possibly satisfy.** A structured ask has no evidence, no charge table and no arms, so
#: `evidence_present` failed at `blocker` severity on every intake — and a blocker that fires on the
#: normal path is a blocker whoever reads it learns to ignore, which is precisely the property the
#: one real blocker (`evidence_present` on a *draft*) depends on. What applies at the request stage
#: is the one check that is about the ask itself: a species the chemist named that will not resolve.
#:
#: **`forbidden_absent` used to be the second of that pair, and it was the same mistake one field
#: over.** The reasoning was "an exclusion the ask contradicts", but an ask that names a species and
#: forbids it is not a contradiction — it is how every solvent-replacement request is phrased. A
#: chemist asking to get out of DMF names DMF as the incumbent and forbids it in one sentence, and
#: that ask reported a *blocker* saying "the design uses reagents the request forbids: DMF" over a
#: design running in 2-MeTHF. The exclusion is still a blocker where it means something — on a
#: design that actually *uses* the species, at the protocol stage, which is the only place a chemist
#: can be harmed by it.
_REQUEST_STAGE: frozenset[str] = frozenset({"components_resolve"})


def run_checks(design: ExperimentDesign, *, stage: CheckStage = "protocol") -> list[ProtocolCheck]:
    """Every check that means something at this stage, in reading order.

    At the `request` stage the protocol-only checks are reported as passing `note`s naming what
    they are waiting for, rather than being omitted: a UI that showed every check on a draft and two
    on a request would look like the checks had been skipped.
    """
    if stage == "protocol":
        return [check(design) for check in _CHECKS]
    return [
        check(design)
        if check.__name__ in _REQUEST_STAGE
        else _ok(check.__name__, "note", "not checked yet — this design holds only the ask")
        for check in _CHECKS
    ]


def blockers(checks: list[ProtocolCheck]) -> list[ProtocolCheck]:
    """The checks that failed at `blocker` severity."""
    return [c for c in checks if c.severity == "blocker" and not c.passed]


def check_ids() -> tuple[str, ...]:
    """Every check id this module produces — what a UI legend and a test enumerate against."""
    return tuple(check.__name__ for check in _CHECKS)
