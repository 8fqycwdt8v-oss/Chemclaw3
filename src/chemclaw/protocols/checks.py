"""The deterministic verdicts a drafted design has to survive.

Computed, never asserted: every function answers from arithmetic, RDKit or the request's own
stated limits, never by asking a model. A prompt can be ignored; a blocker stops the draft from
being stored. Each check is a pure `ExperimentDesign -> ProtocolCheck` run by `run_checks`, so
a new one is a function plus a row in `_CHECKS`.

Severity is per case, not per check. A `blocker` is for designs whose storage would be
misleading: an unreadable structure, a charge table nobody can weigh out, an undeclared level, a
plate that does not fit, a forbidden reagent, no followable evidence. A `warning` is a judgment
about specific work this module is not entitled to make: a missing control, an unmeasured
objective, an unscreened hazard, an implausible setpoint.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from chemclaw.core.chem import InvalidSmilesError, element_counts
from chemclaw.core.reagents import resolve_compound_name
from chemclaw.core.units import UnitError, parse_quantity
from chemclaw.protocols.layout import PLATE_SHAPES, capacity, plate_shape, well_label
from chemclaw.protocols.models import (
    ChargeLine,
    CheckSeverity,
    CheckStage,
    EvidenceRef,
    ExperimentDesign,
    ProtocolCheck,
    RecordedFailure,
    Setpoints,
    UncitedPrecedent,
)
from chemclaw.science.labels.vocabulary import SpeciesRole

#: Temperature outside this band is almost always a unit mistake (Kelvin typed as Celsius). A
#: warning, not a blocker: cryogenic and high-temperature flow work live outside it.
_TEMPERATURE_BAND_C = (-100.0, 300.0)

#: Above this, a "time" is almost certainly minutes typed into an hours field.
_MAX_PLAUSIBLE_HOURS = 336.0

#: The other bands a unit mistake leaves. The models accept any finite value; a bound belongs in a
#: check a chemist can see and overrule, not in a parser.
_MAX_MOLAR = 100.0
_MAX_BAR = 1000.0
_PH_BAND = (-2.0, 16.0)
_MAX_EQUIVALENTS = 200.0
_MAX_MASS_MG = 1_000_000.0
_MAX_VOLUME_ML = 20_000.0

#: How far above the *declared* scale a charge may go before it reads as a unit mistake.
#:
#: The fixed bounds above describe bench work; at kilo scale they would flag every real charge,
#: and a warning that fires on correct input teaches chemists to stop reading warnings. So the
#: bound scales with `request.scale` when one is stated. Loose on purpose: it catches an
#: order-of-magnitude slip, not an unusual recipe.
_SCALE_MASS_MULTIPLE = 1_000.0

#: Litres of any one charge per kilogram of declared scale: several times the widest real process
#: volume, still orders below a mL/L slip.
_SCALE_VOLUMES_PER_KG = 100.0

#: The density assumed to read a volume scale as a mass one, the only physical assumption here.
#: Water, so a scale stated in litres can still widen the mass band.
_ASSUMED_DENSITY_KG_PER_L = 1.0


def _plausibility_bands(design: ExperimentDesign) -> tuple[float, float, str]:
    """The mass (mg) and volume (mL) ceilings for this design, and how they were arrived at.

    Returns the defaults when the request states no readable scale or one in a dimension that fixes
    neither bound (mol). Never tightens: both are `max`ed against the bench defaults, so declaring a
    scale cannot fail a charge that passes without one.
    """
    quantity = parse_quantity(design.request.scale.value)
    if quantity is None:
        return _MAX_MASS_MG, _MAX_VOLUME_ML, ""
    try:
        if quantity.unit.dimension == "mass":
            kilograms = quantity.to("kg").value
        elif quantity.unit.dimension == "volume":
            kilograms = quantity.to("L").value * _ASSUMED_DENSITY_KG_PER_L
        else:
            return _MAX_MASS_MG, _MAX_VOLUME_ML, ""
    except UnitError:  # pragma: no cover - `to` cannot fail on a dimension just matched
        return _MAX_MASS_MG, _MAX_VOLUME_ML, ""
    if kilograms <= 0.0:
        return _MAX_MASS_MG, _MAX_VOLUME_ML, ""
    mass = max(_MAX_MASS_MG, kilograms * 1_000_000.0 * _SCALE_MASS_MULTIPLE)
    volume = max(_MAX_VOLUME_ML, kilograms * _SCALE_VOLUMES_PER_KG * 1_000.0)
    return mass, volume, f" for the declared scale of {design.request.scale.value}"


def _ok(check_id: str, severity: CheckSeverity, detail: str = "") -> ProtocolCheck:
    return ProtocolCheck(check_id=check_id, severity=severity, passed=True, detail=detail)


def _fail(check_id: str, severity: CheckSeverity, detail: str) -> ProtocolCheck:
    return ProtocolCheck(check_id=check_id, severity=severity, passed=False, detail=detail)


def _all_charge_lines(design: ExperimentDesign) -> list[ChargeLine]:
    """Every charge line in the design.

    A seam over the one charge table that several checks read. `charge_is_consistent` and
    `is_a_protocol` read `design.base.charge` directly, so a per-arm override added here would not
    reach them automatically.
    """
    return list(design.base.charge)


def _structures(design: ExperimentDesign) -> list[tuple[str, str]]:
    """Every `(where, smiles)` the design names, so one pass can check them all.

    The reaction SMILES is deliberately excluded: it says what is being asked for, not what the
    design does. A precedent's record form carries the old solvent in its agent slot (which would
    trip `forbidden_absent`), and agents are often written as names. `atom_balance` reads the
    reaction on its own terms.
    """
    asked = [
        (f"request component {component.name_as_written!r}", component.smiles)
        for component in design.request.components
        if component.smiles
    ]
    return [*asked, *used_structures(design)]


def used_structures(design: ExperimentDesign) -> list[tuple[str, str]]:
    """Every `(where, smiles)` the design *does*, as opposed to the ones the ask names.

    A requested component is often the incumbent the chemist wants replaced ("get me out of DMF"),
    so the exclusion checks read this set, not the ask. `components_resolve` still reads the ask,
    because whether a typed name resolves is exactly its question.
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
    # The strict parser alone: `canonical_smiles` is lenient and truncates at whitespace or a
    # non-ASCII character (`"CCO junk"` -> `"CCO"`), so it cannot say whether the input parses.
    bad = [where + f": {smiles!r}" for where, smiles in _structures(design) if not _parses(smiles)]
    if bad:
        return _fail(
            "components_resolve",
            "blocker",
            "these structures do not parse: " + "; ".join(bad),
        )
    named_without_structure = [c.name_as_written for c in design.request.components if not c.smiles]
    if named_without_structure:
        # A *failed* warning: only failed checks are listed to a reader, and an unresolved species
        # is a
        # finding.
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


#: How far a stated amount may sit from the one its equivalents imply, as a fraction of the implied
#: figure: clears ordinary rounding with room to spare while a mis-written digit does not fit.
_AGREEMENT_FRACTION = 0.05

#: The floor under that fraction, in mmol (half a unit in the third decimal), so a correctly
#: rounded trace charge is never a blocker.
_AGREEMENT_FLOOR_MMOL = 0.0005


def _agreement_tolerance(implied_mmol: float) -> float:
    """How far a stated amount may sit from the one its equivalents imply.

    A twentieth of the *implied* amount, floored at half a unit in the third decimal. Derived from
    nothing the chemist typed: a tolerance on the line's own figure blocks rounded catalyst lines,
    and a float cannot carry the typed precision (`0.10` is `0.1`). The sweep in
    `tests/test_protocol_checks.py` holds the verdicts across bench scales and loadings.
    """
    return max(_AGREEMENT_FRACTION * abs(implied_mmol), _AGREEMENT_FLOOR_MMOL)


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
    # `not` rather than `is None`: a limiting reagent at `0.0` gives no scale to weigh against, the
    # same as absent.
    if not reference.amount_mmol:
        return _fail(
            "charge_is_consistent",
            "warning",
            f"the limiting reagent {reference.component!r} has no usable amount "
            f"({reference.amount_mmol!r}), so no other line's equivalents can be turned into a "
            "weight",
        )
    # Equivalents and amounts are two statements of one fact; a table where they disagree will be
    # weighed out wrong. See `_agreement_tolerance`.
    disagreements = [
        f"{line.component!r}: {line.equivalents} eq implies "
        f"{line.equivalents * reference.amount_mmol:.4g} mmol, table says {line.amount_mmol:.4g}"
        for line in lines
        if line.equivalents is not None
        and line.amount_mmol is not None
        # Relative slack on the float comparison itself, so a table exactly on its tolerance is not
        # refused by binary rounding.
        and abs(line.equivalents * reference.amount_mmol - line.amount_mmol)
        > _agreement_tolerance(line.equivalents * reference.amount_mmol) * (1 + 1e-9)
    ]
    if disagreements:
        return _fail(
            "charge_is_consistent",
            "blocker",
            "equivalents and amounts disagree by more than "
            f"{_AGREEMENT_FRACTION:.0%}: " + "; ".join(disagreements),
        )
    return _ok(
        "charge_is_consistent",
        "blocker",
        f"limiting reagent {reference.component!r} at {reference.amount_mmol:.4g} mmol",
    )


def limiting_is_limiting(design: ExperimentDesign) -> ProtocolCheck:
    """The line marked limiting is the one that actually runs out first.

    `charge_is_consistent` does not check that the reference is the *minimum*, so a self-consistent
    table can name the wrong one and over-report every yield stated against it. A warning, since a
    deliberately sub-stoichiometric reagent is unusual rather than impossible. Only
    `starting-material` and `reagent` lines are weighed. On an unlabelled table (`role` defaults to
    `UNKNOWN`) nothing is weighed, and the passing verdict says so.
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
    weighed = [
        line
        for line in lines
        if line is not reference and line.role in stoichiometric and line.amount_mmol is not None
    ]
    if not weighed:
        return _ok(
            "limiting_is_limiting",
            "warning",
            f"nothing was weighed against {reference.component!r}: no other line carries a "
            "starting-material or reagent role with an amount. Label the charge table's roles and "
            "this check can tell you whether the right line is marked limiting",
        )
    return _ok(
        "limiting_is_limiting",
        "warning",
        f"{reference.component!r} at {reference_mmol:.4g} mmol is the smallest of the "
        f"{len(weighed) + 1} stoichiometric charges",
    )


def atom_balance(design: ExperimentDesign) -> ProtocolCheck:
    """No expected product contains an element nothing charged supplies."""
    # The element-balance rule `ingest.eln.validate.validate_ord` applies to a recorded reaction.
    # Counts are not compared (no stoichiometric coefficients). Both `a>>c` and the record form
    # `a>b>c` are accepted by splitting on `>`; agents supply elements, so they join the inputs.
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

    A **failed** warning, so the sentence naming the species reaches the page. Not a blocker: an
    unreadable structure is `components_resolve`'s blocker to raise.
    """
    return _fail("atom_balance", "warning", f"could not read {smiles!r}; balance not checked")


def _split_species(side: str) -> list[str]:
    """The species in one block of a reaction SMILES."""
    return [part for part in side.split(".") if part]


def factor_levels_declared(design: ExperimentDesign) -> ProtocolCheck:
    """Every arm sets levels its factors declare, and sets all of them."""
    declared = {f.name: {level.label for level in f.levels} for f in design.factors}
    if not design.factors:
        # Not an early return: with no factors declared, every level an arm sets is undeclared.
        stray = sorted({name for arm in design.arms for name in arm.levels})
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
        unknown = sorted(set(arm.levels) - set(declared))
        if unknown:
            problems.append(f"{arm.arm_id} sets undeclared factor(s): {', '.join(unknown)}")
        if arm.control:
            # A control may hold a level outside the factor *space* and may leave factors unset, but
            # its
            # level names must still be declared factors: the run sheet builds its columns from
            # `design.factors`, so an undeclared name would reach no page. A control that differs
            # otherwise
            # says so in `note`.
            continue
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
    # Setpoints are part of the conditions: arms differing only in temperature are not duplicates,
    # and `replicate_of` would refuse them.
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
        # The design's own shape, not `request.mode`: the ask's mode is not tied to what was
        # drafted.
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
    # Counted, not set-compared: "every arm once" must catch an arm placed twice as well as one
    # missing.
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
    # Check the plate's own shape and each well's position: a layout can arrive whole from the API,
    # so only `place()` output is trusted.
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
    # The blocker that makes "use the record and the tools" a property of the code: a design citing
    # neither is a guess. A citation counts only when followable (`kind="tool"` names a tool,
    # `kind="precedent"` carries a `ref`), so a chemist has something to open.
    cited = {ref.kind for ref in design.evidence if _is_followable(ref)}
    unfollowable = [ref.summary for ref in design.evidence if not _is_followable(ref)]
    kinds = cited
    grounded = kinds & {"precedent", "record", "note", "observation"}
    if not grounded and "tool" not in kinds:
        return _fail(
            "evidence_present",
            "blocker",
            (
                # Say which failure this is: unfollowable citations, not missing ones.
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
    # A step's own temperature and duration are setpoints too.
    for index, step in enumerate(design.base.steps, start=1):
        if step.temperature_c is not None and not low <= step.temperature_c <= high:
            problems.append(f"step {index}: {step.temperature_c} °C is outside {low}..{high}")
        if step.duration_h is not None and step.duration_h > _MAX_PLAUSIBLE_HOURS:
            problems.append(
                f"step {index}: {step.duration_h} h is over {_MAX_PLAUSIBLE_HOURS:.0f} h"
            )
    max_mass_mg, max_volume_ml, basis = _plausibility_bands(design)
    for line in _all_charge_lines(design):
        if line.equivalents == 0.0 and not line.limiting:
            problems.append(f"charge line {line.component!r} is 0 equivalents")
        if line.equivalents is not None and line.equivalents > _MAX_EQUIVALENTS:
            problems.append(
                f"charge line {line.component!r} at {line.equivalents} equivalents is over "
                f"{_MAX_EQUIVALENTS:.0f} — a solvent is charged by volume, not by equivalents"
            )
        if line.mass_mg is not None and line.mass_mg > max_mass_mg:
            problems.append(
                f"charge line {line.component!r}: {line.mass_mg} mg is over "
                f"{max_mass_mg / 1_000_000.0:g} kg{basis}"
            )
        if line.volume_ml is not None and line.volume_ml > max_volume_ml:
            problems.append(
                f"charge line {line.component!r}: {line.volume_ml} mL is over "
                f"{max_volume_ml / 1_000.0:g} L{basis}"
            )
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
    # Both sides go through `core.reagents.resolve_compound_name`, so forbidding "DMF" also catches
    # `N,N-dimethylformamide` (RDKit cannot read a name). The written names are still compared for
    # reagents the table does not carry.
    names = {n.strip().lower() for n in _used_species(design) if n.strip()}
    structures = {_identity(value) for value in (*names, *(s for _, s in used_structures(design)))}
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

    The fallback keeps reagents the curated table lacks (internal codes) usable by spelling;
    `resolve_compound_name` never guesses, so a miss is never a fabricated structure.
    """
    resolved = resolve_compound_name(value.strip())
    return resolved.smiles if resolved is not None else value.strip().lower()


def _used_species(design: ExperimentDesign) -> list[str]:
    """Every human-readable species name the design *uses*.

    Not the ask's `components` (see `used_structures`). Includes the solvent and its per-arm
    override (the commonest hard exclusion) and each step's components, since a procedure can name a
    reagent the charge table omits.
    """
    names = [line.component for line in _all_charge_lines(design)]
    names += [level.label for factor in design.factors for level in factor.levels]
    names += [points.solvent for _, points in _all_setpoints(design)]
    names += [points.atmosphere for _, points in _all_setpoints(design)]
    names += [component for step in design.base.steps for component in step.components]
    return names


def coverage_is_stated(design: ExperimentDesign) -> ProtocolCheck:
    """A screen either covers its factor grid or says how much of it it covers."""
    # Only `campaign` is exempt: a campaign may ship a first round that does not cover its factor
    # space. The shape decides the rest.
    if design.request.mode == "campaign" or not design.factors:
        return _ok("coverage_is_stated", "note", "not a fixed screen")
    full = 1
    for factor in design.factors:
        full *= len(factor.levels)
    # Distinct level combinations, not arms: arms can repeat a combination. An arm leaving a factor
    # unset covers no combination.
    names = [factor.name for factor in design.factors]
    covered = {
        tuple(arm.levels.get(name, "") for name in names)
        for arm in design.arms
        if not arm.control and not arm.replicate_of
    }
    real = len([combination for combination in covered if all(combination)])
    if real >= full:
        return _ok("coverage_is_stated", "note", f"full grid: {real} of {full} combinations")
    # Passing, but the note still reaches the page: a fractional factorial is a deliberate design
    # and
    # nothing in `ExperimentDesign` records the confounding statement a failure would ask for.
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
def no_documented_failure(
    design: ExperimentDesign, failures: Sequence[RecordedFailure] = ()
) -> ProtocolCheck:
    """Nothing this design rests on has already been recorded as having failed.

    `forbidden_absent` tests what the chemist typed; this tests what the corpus knows, so a design
    cannot silently repeat a documented `failure-mode` note. A `note`, not a blocker: a recorded
    failure is evidence with a confidence, and deliberately re-running a failure is ordinary work.
    Pure over what the caller supplies, because checks are synchronous and the corpus is not.

    Args:
        design: The design being checked.
        failures: What `failures_against` found. Empty means nothing was found *or* nobody looked;
            this cannot tell them apart (see `run_checks`).

    Returns:
        A passing `note` when nothing bears on it, and a failing one naming what to read.
    """
    if not failures:
        return _ok("no_documented_failure", "note", "no recorded failure bears on this design")
    named = "; ".join(
        f"{failure.id} ({failure.summary})" if failure.summary else failure.id
        for failure in failures[:_MAX_NAMED_FAILURES]
    )
    more = len(failures) - _MAX_NAMED_FAILURES
    tail = f", and {more} more" if more > 0 else ""
    return _fail(
        "no_documented_failure",
        "note",
        f"the corpus records {len(failures)} failure(s) bearing on this design: {named}{tail}",
    )


def precedent_consulted(
    design: ExperimentDesign, precedent: Sequence[UncitedPrecedent] = ()
) -> ProtocolCheck:
    """The record holds runs like this one, and this design cites none of them.

    Advisory, and it never writes into `evidence`: turning a search hit into a citation would let
    `evidence_present` pass on a design nobody grounded. A `note`, since a structural neighbour is
    not automatically relevant. An empty input is silence, not a claimed negative: the caller passes
    only hits it stands behind.

    Args:
        design: The design being checked.
        precedent: Similar runs the record holds that this design does not cite, as
            `agent.protocol_design_tools.uncited_precedent` reduced them.

    Returns:
        A passing `note` when there is nothing to offer, and a failing one naming what to read.
    """
    if not precedent:
        return _ok(
            "precedent_consulted", "note", "no uncited precedent was offered for this design"
        )
    named = "; ".join(
        f"{hit.id} ({hit.similarity:.2f})" for hit in precedent[:_MAX_NAMED_PRECEDENT]
    )
    more = len(precedent) - _MAX_NAMED_PRECEDENT
    tail = f", and {more} more" if more > 0 else ""
    return _fail(
        "precedent_consulted",
        "note",
        f"the record holds {len(precedent)} similar run(s) this design does not cite: {named}"
        f"{tail}. Read them before running this — or say why they do not apply",
    )


#: How many precedents to name before the detail is the problem. A design that ignored twenty near
#: neighbours has one thing wrong with it, and the count still reports the rest.
_MAX_NAMED_PRECEDENT = 3


#: How many failures to name; the count still reports the rest.
_MAX_NAMED_FAILURES = 3


# Corpus-fed checks are registered here too, so the id test holds the registry to what
# `run_checks` produces in both directions.
_CHECKS: tuple[Callable[..., ProtocolCheck], ...] = (
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
    no_documented_failure,
    precedent_consulted,
    coverage_is_stated,
)

#: The checks that mean anything about a design holding only the structured ask; the rest are
#: questions about a procedure that does not exist yet.
#:
#: A blocker that fires on every intake teaches readers to ignore blockers, so only checks about
#: the ask itself apply here. `forbidden_absent` is not one: an ask naming the incumbent it
#: forbids is how a replacement request is phrased, and the exclusion bites on the protocol. The
#: precedent and failure checks apply because the ask already names a reaction and reagents.
_REQUEST_STAGE: frozenset[str] = frozenset(
    {"components_resolve", "no_documented_failure", "precedent_consulted"}
)


def run_checks(
    design: ExperimentDesign,
    *,
    stage: CheckStage = "protocol",
    failures: Sequence[RecordedFailure] = (),
    precedent: Sequence[UncitedPrecedent] = (),
) -> list[ProtocolCheck]:
    """Every check that means something at this stage, in reading order.

    At the `request` stage the protocol-only checks are reported as passing notes naming what they
    wait for, rather than omitted, so a request does not look under-checked.

    `failures` and `precedent` come from the corpus, not the design: this function is synchronous,
    so the caller does the lookup and passes what it found. An empty `failures` cannot distinguish
    "nothing bears on it" from "nobody looked", so a caller that skips the lookup publishes a clean
    bill the corpus never gave. Both corpus checks run at both stages; the dispatch is a mapping so
    a
    further corpus-fed check needs no edit here.
    """
    supplied: dict[Callable[..., ProtocolCheck], Sequence[Any]] = {
        no_documented_failure: failures,
        precedent_consulted: precedent,
    }

    # The stage gate is asked first; otherwise a supplied check would run at both stages whatever
    # `_REQUEST_STAGE` says.
    def run_one(check: Callable[..., ProtocolCheck]) -> ProtocolCheck:
        """One check, stage-gated first and only then dispatched."""
        if stage != "protocol" and check.__name__ not in _REQUEST_STAGE:
            return _ok(check.__name__, "note", "not checked yet — this design holds only the ask")
        if check in supplied:
            return check(design, supplied[check])
        return check(design)

    return [run_one(check) for check in _CHECKS]


def blockers(checks: list[ProtocolCheck]) -> list[ProtocolCheck]:
    """The checks that failed at `blocker` severity."""
    return [c for c in checks if c.severity == "blocker" and not c.passed]
