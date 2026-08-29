"""The deterministic verdicts a drafted design has to survive.

**These are computed, never asserted.** Nothing here asks a model whether its own protocol is
sound; every function reads the design and answers from arithmetic, RDKit, or the request's own
stated limits. That is the whole reason this file exists rather than a paragraph in a `SKILL.md`:
a prompt can ask for a hazard screen and a prompt can be ignored, while
`evidence_present` returning a blocker stops the draft from being stored at all.

Each check is a pure `ExperimentDesign -> ProtocolCheck` and they are run as a set by `run_checks`,
so a new one is a function plus a row in `_CHECKS` and nothing else moves.

**Severity is the design decision in this file, and it is not uniform.** A `blocker` refuses the
draft, so it is reserved for the cases where storing the design would be storing something
misleading: a structure nobody can read, a charge table with no limiting reagent, a plate that does
not fit, an arm setting a level the factor does not declare, a reagent the chemist forbade, and a
design with no evidence at all. Everything else is a `warning` a chemist reads and overrules — a
missing control, an unmeasured objective, a hazard screen nobody ran — because those are judgments
about a specific piece of work and this file is not entitled to make them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from chemclaw.core.chem import InvalidSmilesError, canonical_smiles, element_counts
from chemclaw.protocols.layout import PLATE_SHAPES, capacity
from chemclaw.protocols.models import (
    ChargeLine,
    CheckSeverity,
    CheckStage,
    ExperimentDesign,
    ProtocolCheck,
    Setpoints,
)

#: Temperature outside this band is almost always a unit mistake (a Kelvin value typed into a
#: Celsius field reads as 353 °C) rather than a real setpoint. A warning, not a blocker: sub-zero
#: cryogenic work and high-temperature flow chemistry both live outside a narrow band.
_TEMPERATURE_BAND_C = (-100.0, 300.0)

#: Above this, a "time" is almost certainly minutes typed into an hours field.
_MAX_PLAUSIBLE_HOURS = 336.0


def _ok(check_id: str, severity: CheckSeverity, detail: str = "") -> ProtocolCheck:
    return ProtocolCheck(check_id=check_id, severity=severity, passed=True, detail=detail)


def _fail(check_id: str, severity: CheckSeverity, detail: str) -> ProtocolCheck:
    return ProtocolCheck(check_id=check_id, severity=severity, passed=False, detail=detail)


def _all_charge_lines(design: ExperimentDesign) -> list[ChargeLine]:
    """Every charge line in the design.

    A function rather than `design.base.charge` inline, because there is exactly one charge table
    today and four checks read it: if a per-arm override is ever argued for, it lands here and none
    of them changes.
    """
    return list(design.base.charge)


def _structures(design: ExperimentDesign) -> list[tuple[str, str]]:
    """Every `(where, smiles)` the design names, so one pass can check them all."""
    found: list[tuple[str, str]] = []
    for component in design.request.components:
        if component.smiles:
            found.append((f"request component {component.name_as_written!r}", component.smiles))
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
    bad = []
    for where, smiles in _structures(design):
        try:
            canonical = canonical_smiles(smiles)
        except InvalidSmilesError as exc:  # pragma: no cover - canonical_smiles is lenient
            bad.append(f"{where}: {exc}")
            continue
        # `canonical_smiles` is deliberately lenient — it hands back the input unchanged when RDKit
        # cannot read it — so an unchanged string that RDKit would not re-read is the tell.
        if canonical == smiles and not _parses(smiles):
            bad.append(f"{where}: {smiles!r}")
    if bad:
        return _fail(
            "components_resolve",
            "blocker",
            "these structures do not parse: " + "; ".join(bad),
        )
    named_without_structure = [c.name_as_written for c in design.request.components if not c.smiles]
    if named_without_structure:
        return _ok(
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
    if reference.amount_mmol is None:
        return _ok(
            "charge_is_consistent",
            "warning",
            f"the limiting reagent {reference.component!r} has no amount, so no other line's "
            "equivalents can be turned into a weight",
        )
    # Equivalents and amounts are two statements of the same fact, and a table where they disagree
    # is one a chemist will weigh out wrong. Checked to 2%: a charge table is rounded to sensible
    # weights and an exact comparison would fail on every real protocol.
    disagreements = [
        f"{line.component!r}: {line.equivalents} eq implies "
        f"{line.equivalents * reference.amount_mmol:.4g} mmol, table says {line.amount_mmol:.4g}"
        for line in lines
        if line.equivalents is not None
        and line.amount_mmol is not None
        and reference.amount_mmol > 0
        and abs(line.equivalents * reference.amount_mmol - line.amount_mmol)
        > 0.02 * max(line.amount_mmol, 1e-9)
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


def atom_balance(design: ExperimentDesign) -> ProtocolCheck:
    """No expected product contains an element nothing charged supplies."""
    # The same rule `ingest.eln.validate.validate_ord` applies to a recorded reaction, asked of a
    # proposed one. Counts are deliberately not compared, for that function's reason: there are no
    # stoichiometric coefficients here either, so a dimerization would fail a per-molecule count.
    reaction = design.request.reaction_smiles.strip()
    if ">>" not in reaction:
        return _ok("atom_balance", "warning", "no reaction SMILES to balance")
    reactant_side, _, product_side = reaction.partition(">>")
    supplied: set[str] = set()
    for smiles in _split_species(reactant_side) + [
        line.smiles for line in _all_charge_lines(design)
    ]:
        if not smiles:
            continue
        try:
            supplied.update(element_counts(smiles))
        except InvalidSmilesError:
            return _ok("atom_balance", "warning", f"could not read {smiles!r}; balance not checked")
    missing: set[str] = set()
    for smiles in _split_species(product_side):
        try:
            missing.update(set(element_counts(smiles)) - supplied)
        except InvalidSmilesError:
            return _ok("atom_balance", "warning", f"could not read {smiles!r}; balance not checked")
    if missing:
        return _fail(
            "atom_balance",
            "warning",
            "the product contains elements nothing charged supplies: "
            + ", ".join(sorted(missing))
            + " — either a species is missing from the charge table or the product is wrong",
        )
    return _ok("atom_balance", "warning", "every product element is supplied")


def _split_species(side: str) -> list[str]:
    """The species on one side of a reaction SMILES, agents included."""
    return [part for chunk in side.split(">") for part in chunk.split(".") if part]


def factor_levels_declared(design: ExperimentDesign) -> ProtocolCheck:
    """Every arm sets levels its factors declare, and sets all of them."""
    if not design.factors:
        return _ok("factor_levels_declared", "blocker", "no factors")
    declared = {f.name: {level.label for level in f.levels} for f in design.factors}
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
    seen: dict[tuple[tuple[str, str], ...], str] = {}
    duplicates: list[str] = []
    for arm in design.arms:
        if arm.replicate_of or arm.control:
            continue
        key = tuple(sorted(arm.levels.items()))
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
        if design.request.mode == "single":
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
    placed = {w.arm_id for w in layout.wells}
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
    return _ok(
        "layout_fits",
        "blocker",
        f"{len(layout.wells)} of {capacity(layout.plate_format)} wells used",
    )


def controls_present(design: ExperimentDesign) -> ProtocolCheck:
    """A plate carries at least one control."""
    if design.request.mode == "single":
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
    kinds = {ref.kind for ref in design.evidence}
    grounded = kinds & {"precedent", "record", "note", "observation"}
    if not grounded and "tool" not in kinds:
        return _fail(
            "evidence_present",
            "blocker",
            "this design cites nothing. Search the record (substrate_precedent, "
            "conditions_for_similar_reaction, reagent_frequency, similar_reactions, "
            "gather_evidence) and compute what it does not state, then cite what you used in "
            "`evidence`",
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
    return _ok(
        "evidence_present",
        "blocker",
        f"{len(design.evidence)} citations across {', '.join(sorted(kinds))}",
    )


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
    for line in _all_charge_lines(design):
        if line.equivalents == 0.0 and not line.limiting:
            problems.append(f"charge line {line.component!r} is 0 equivalents")
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
    # Matched on both the written name and the canonical structure, because a chemist writes
    # "DMF" and a design writes `CN(C)C=O`, and refusing to look at the structure would let the
    # exclusion be defeated by spelling.
    names = {n.lower() for n in _named_species(design)}
    structures = {_canonical_or_input(s) for _, s in _structures(design)}
    hits = [
        term
        for term in forbidden
        if term.lower() in names or _canonical_or_input(term) in structures
    ]
    if hits:
        return _fail(
            "forbidden_absent",
            "blocker",
            "the design uses reagents the request forbids: " + ", ".join(hits),
        )
    return _ok("forbidden_absent", "blocker", f"{len(forbidden)} exclusions honoured")


def _canonical_or_input(value: str) -> str:
    try:
        return canonical_smiles(value)
    except InvalidSmilesError:  # pragma: no cover - canonical_smiles is lenient
        return value


def _named_species(design: ExperimentDesign) -> list[str]:
    """Every human-readable species name the design mentions."""
    names = [c.name_as_written for c in design.request.components]
    names += [line.component for line in _all_charge_lines(design)]
    names += [level.label for factor in design.factors for level in factor.levels]
    return names


def coverage_is_stated(design: ExperimentDesign) -> ProtocolCheck:
    """A screen either covers its factor grid or says how much of it it covers."""
    if design.request.mode != "screen" or not design.factors:
        return _ok("coverage_is_stated", "note", "not a fixed screen")
    full = 1
    for factor in design.factors:
        full *= len(factor.levels)
    real = len([a for a in design.arms if not a.control and not a.replicate_of])
    if real >= full:
        return _ok("coverage_is_stated", "note", f"full grid: {real} of {full} combinations")
    return _ok(
        "coverage_is_stated",
        "note",
        f"reduced design: {real} of {full} combinations. Say which combinations were given up and "
        "which effects are therefore confounded — a fractional design presented as the whole "
        "screen is how a plate gets over-read",
    )


def is_a_protocol(design: ExperimentDesign) -> ProtocolCheck:
    """The design says what to do — it has at least one arm or one step."""
    if design.arms or design.base.steps or design.base.charge:
        return _ok(
            "is_a_protocol",
            "blocker",
            f"{len(design.arms)} arm(s), {len(design.base.steps)} step(s)",
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
#: one real blocker (`evidence_present` on a *draft*) depends on. What *does* apply at the request
#: stage is the pair that is about the ask itself: a species that will not resolve, and an exclusion
#: the ask contradicts.
_REQUEST_STAGE: frozenset[str] = frozenset({"components_resolve", "forbidden_absent"})


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
