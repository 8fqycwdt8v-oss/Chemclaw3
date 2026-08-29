"""What a design looks like to the three readers it has: a model, a browser and a chemist.

**One payload serves the model and the browser, and it is JSON.** A tool that returned Markdown
would leave the front end with a blob it cannot parse — `ResultBlock` in `Chemclaw3_ui` does
`JSON.parse` and falls back to rendering *nothing* — so the choice is not "prose or JSON", it is
"one payload both readers use, or two serializations of one design that can disagree". The measured
context argument does not push the other way either:
`D-2026-08-27-a-tool-result-crosses-a-boundary-and-must-say-so` found compact JSON 0.4–0.8%
*shorter* than the pydantic repr a returned model would
become, and declined a blanket switch because it would have been an edit to every tool at once —
not because JSON was worse. This is one tool, choosing its own return.

**The receipt is deliberately not the whole design.** A model that has just authored a protocol
does not need it echoed back; it needs to know the design was stored, under what id, at what
revision, and what the checks said. The whole document is one `read_experiment_protocol` away and
is what `GET /protocols/{id}` serves. That is the difference between a receipt and a reply.

`render_markdown` is the third reader — a chemist reading a protocol as a document, in a report or
a note body. It is not what a tool returns.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.protocols.models import (
    DesignStatus,
    DesignSummary,
    ExperimentDesign,
    FactorLevel,
    ProtocolArm,
    ProtocolCheck,
    ProtocolStep,
    Setpoints,
)

#: How many arms a receipt lists before it stops and says how many are left. A 384-well design's
#: whole arm table is not what a model needs back from a write it just made, and it is the largest
#: thing in this payload by an order of magnitude.
_RECEIPT_ARMS = 12


class ArmRow(BaseModel):
    """One arm as a table row — what a run sheet and a plate map both read."""

    arm_id: str
    well: str = ""
    run_order: int = 0
    levels: dict[str, str] = Field(default_factory=dict)
    temperature_c: float | None = None
    time_h: float | None = None
    solvent: str = ""
    control: str = ""
    replicate_of: str = ""
    note: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ProtocolReceipt(BaseModel):
    """What a draft or a revision hands back: where it was stored and what the checks said."""

    design_id: str
    revision: int
    title: str
    mode: str
    status: DesignStatus
    # One sentence a model can quote to the chemist without re-reading the design.
    summary: str
    checks: list[ProtocolCheck] = Field(default_factory=list)
    blocking: list[str] = Field(default_factory=list)
    factors: dict[str, list[str]] = Field(default_factory=dict)
    arm_count: int = 0
    arms: list[ArmRow] = Field(default_factory=list)
    arms_omitted: int = 0
    plate_format: int = 0
    evidence_count: int = 0
    # The paths a human changed in the revision this receipt is for, when it revises another.
    changed_paths: list[str] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="forbid")


class ProtocolReadout(BaseModel):
    """What a *read* hands back: the receipt, the whole document, and the document as prose.

    Three forms of one design, deliberately. The receipt is what a model quotes, the document is
    what the browser renders and what a revision is derived from, and the Markdown is what a report
    or a note body carries — rebuilding that third form from the second in a turn would produce a
    different rendering every time, which is how two descriptions of one protocol come to disagree.
    """

    receipt: ProtocolReceipt
    design: ExperimentDesign
    markdown: str

    model_config = ConfigDict(frozen=True, extra="forbid")


class DesignListing(BaseModel):
    """A page of designs."""

    designs: list[DesignSummary] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="forbid")


def _points(design: ExperimentDesign, arm: ProtocolArm) -> Setpoints:
    return design.setpoints_for(arm)


def run_sheet_rows(design: ExperimentDesign) -> list[ArmRow]:
    """Every arm as a row, in run order when there is a layout and in arm order otherwise."""
    wells = {well.arm_id: well for well in (design.layout.wells if design.layout else [])}
    rows = [
        ArmRow(
            arm_id=arm.arm_id,
            well=wells[arm.arm_id].label if arm.arm_id in wells else "",
            run_order=wells[arm.arm_id].run_order if arm.arm_id in wells else 0,
            levels=dict(arm.levels),
            temperature_c=_points(design, arm).temperature_c,
            time_h=_points(design, arm).time_h,
            solvent=_points(design, arm).solvent,
            control=arm.control,
            replicate_of=arm.replicate_of,
            note=arm.note,
        )
        for arm in design.arms
    ]
    # A randomised design's whole point is that it is *run* in an order the plate does not show, so
    # the run sheet is sorted by that order and the plate map is what shows position.
    return sorted(rows, key=lambda r: (r.run_order == 0, r.run_order)) if wells else rows


def summarise(design: ExperimentDesign, checks: list[ProtocolCheck]) -> str:
    """The one sentence that says what this design is and whether it is runnable."""
    failed = [c for c in checks if not c.passed]
    blocking = [c for c in failed if c.severity == "blocker"]
    if not design.has_protocol:
        # A design holding only the structured ask. Saying "0 arms over 0 factors" here would read
        # as an empty protocol rather than as an intake nobody has drafted yet. `has_protocol`
        # rather than the condition spelled out again: this was the fourth caller deciding it
        # separately, in the release whose own note says that is how the second and third got it
        # wrong.
        shape = "the structured ask, no procedure yet"
    elif len(design.arms) <= 1 and not design.factors:
        # **The design, not the ask.** This branched on `request.mode`, so a 4-arm 2-factor plate
        # whose ask still said `single` summarised as "1 experiment" — in the one sentence a model
        # is told it can quote to a chemist without re-reading the design. Nothing forces `mode` to
        # match the arms, so the sentence has to read them.
        shape = "1 experiment"
    else:
        controls = sum(1 for a in design.arms if a.control)
        shape = f"{len(design.arms)} arms over {len(design.factors)} factors"
        if controls:
            shape += f" plus {controls} control(s)"
        if design.layout:
            shape += f" on a {design.layout.plate_format}-well plate"
    verdict = (
        f"{len(blocking)} blocking check(s)"
        if blocking
        else f"no blocking checks, {len(failed)} warning(s)"
    )
    return f"{design.request.title}: {shape}; {verdict}; {len(design.evidence)} citations."


def receipt(
    design: ExperimentDesign,
    checks: list[ProtocolCheck],
    *,
    design_id: str,
    revision: int,
    status: DesignStatus,
    changed_paths: list[str] | None = None,
) -> ProtocolReceipt:
    """The payload a write hands back to the model and to the browser."""
    rows = run_sheet_rows(design)
    return ProtocolReceipt(
        design_id=design_id,
        revision=revision,
        title=design.request.title,
        mode=design.request.mode,
        status=status,
        summary=summarise(design, checks),
        checks=checks,
        blocking=[c.check_id for c in checks if c.severity == "blocker" and not c.passed],
        factors={f.name: [level.label for level in f.levels] for f in design.factors},
        arm_count=len(design.arms),
        arms=rows[:_RECEIPT_ARMS],
        arms_omitted=max(0, len(rows) - _RECEIPT_ARMS),
        plate_format=design.layout.plate_format if design.layout else 0,
        evidence_count=len(design.evidence),
        changed_paths=list(changed_paths or []),
    )


def _table(headers: list[str], rows: list[list[str]]) -> str:
    """A GitHub-flavoured Markdown table, or an empty string when there are no rows."""
    if not rows:
        return ""
    head = "| " + " | ".join(headers) + " |"
    rule = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(cell or "—" for cell in row) + " |" for row in rows]
    return "\n".join([head, rule, *body])


def _number(value: float | None) -> str:
    # `.6g` is `%g`'s default and turns a kilogram-scale charge into `1.23457e+06` mg. `.10g` keeps
    # every weight a laboratory can measure exact while still trimming a float's tail.
    return "" if value is None else f"{value:.10g}"


def _level(level: FactorLevel) -> str:
    """One level as the Factors table shows it — its label, and its value when it has one."""
    return level.label if level.value is None else f"{level.label} ({_number(level.value)})"


def _step_conditions(step: ProtocolStep) -> str:
    """A step's own temperature and duration, appended to its line when it states them."""
    stated = [
        f"{_number(step.temperature_c)} °C" if step.temperature_c is not None else "",
        f"{_number(step.duration_h)} h" if step.duration_h is not None else "",
    ]
    said = [part for part in stated if part]
    return f" — {', '.join(said)}" if said else ""


def render_markdown(design: ExperimentDesign, checks: list[ProtocolCheck] | None = None) -> str:
    """The design as a document a chemist reads — the form a report or a note body carries.

    Deliberately not what a tool returns (see the module docstring): this is for a human reader and
    for anything that renders one, and it is lossy in the direction a document should be — it shows
    the shared body once and the arms as a table, rather than N protocols.
    """
    request = design.request
    parts: list[str] = [f"# {request.title}", "", f"**Goal.** {request.goal}", ""]
    if request.reaction_smiles:
        parts += [f"**Transformation.** `{request.reaction_smiles}`", ""]
    if request.objectives:
        parts += ["**Objectives.** " + ", ".join(request.objectives), ""]
    if request.forbidden:
        # The chemist's hard exclusions, whose violation is a blocker — and which the document a
        # chemist reads did not mention.
        parts += ["**Ruled out.** " + ", ".join(request.forbidden), ""]

    # **The conditions of the arm, when there is exactly one.** `## Conditions` rendered
    # `base.setpoints` unconditionally and the run sheet was gated on `len(rows) > 1 or factors`,
    # so a single experiment whose arm overrode the body got neither: the document said 80 °C /
    # 16 h / dioxane for an arm the design runs at 120 °C / 2 h / toluene, with every blocker
    # passing and nothing on the page hinting a second set of conditions existed. This is a
    # document a chemist runs from.
    solo = design.arms[0] if len(design.arms) == 1 and not design.factors else None
    points = design.setpoints_for(solo) if solo is not None else design.base.setpoints
    stated = [
        (label, value)
        for label, value in (
            (
                "Temperature",
                f"{_number(points.temperature_c)} °C" if points.temperature_c is not None else "",
            ),
            ("Time", f"{_number(points.time_h)} h" if points.time_h is not None else ""),
            ("Solvent", points.solvent),
            (
                "Concentration",
                f"{_number(points.concentration_molar)} M"
                if points.concentration_molar is not None
                else "",
            ),
            ("Atmosphere", points.atmosphere),
            (
                "Pressure",
                f"{_number(points.pressure_bar)} bar" if points.pressure_bar is not None else "",
            ),
            # A stated pH was dropped from the document entirely — a `ProtocolBody` field with no
            # reader, in the section whose whole subject is the conditions.
            ("pH", _number(points.ph)),
        )
        if value
    ]
    if stated:
        heading = "## Conditions" if solo is None else f"## Conditions ({solo.arm_id})"
        parts += [heading, "", *[f"- **{k}:** {v}" for k, v in stated], ""]
        if solo is not None and solo.note:
            parts += [f"*{solo.note}*", ""]

    charge = _table(
        ["Component", "Role", "Equiv", "mmol", "mg", "mL", "Note"],
        [
            [
                line.component + (" *(limiting)*" if line.limiting else ""),
                str(line.role),
                _number(line.equivalents),
                _number(line.amount_mmol),
                _number(line.mass_mg),
                _number(line.volume_ml),
                line.note,
            ]
            for line in design.base.charge
        ],
    )
    if charge:
        parts += ["## Charge", "", charge, ""]

    if design.base.steps:
        parts += ["## Procedure", ""]
        # A step's own temperature and hold time were dropped, which is the pair a step most often
        # carries and the pair a chemist reads off the page while running it.
        parts += [
            f"{step.index}. *({step.kind})* {step.text}" + _step_conditions(step)
            for step in design.base.steps
        ]
        parts += [""]

    if design.factors:
        parts += [
            "## Factors",
            "",
            _table(
                # `Unit` and the per-level rationale were both dropped. Every other number in this
                # document carries a unit, which is exactly what makes a bare `1` in a levels
                # column read as an equivalent — and `FactorLevel.rationale` is what `models.py`
                # calls "the single most useful sentence on a screening plate".
                ["Factor", "Kind", "Role", "Unit", "Levels", "Why"],
                [
                    [
                        f.name,
                        f.kind,
                        str(f.role),
                        f.unit,
                        ", ".join(_level(level) for level in f.levels),
                        "; ".join(level.rationale for level in f.levels if level.rationale),
                    ]
                    for f in design.factors
                ],
            ),
            "",
        ]

    rows = run_sheet_rows(design)
    # Every design with arms gets a run sheet. The old gate hid it for exactly the design that
    # needed it most — one arm, no factors, its own setpoints — see `solo` above.
    if len(rows) > 1 or design.factors:
        factor_names = [f.name for f in design.factors]
        parts += [
            "## Run sheet",
            "",
            _table(
                ["Run", "Well", "Arm", *factor_names, "T /°C", "t /h", "Solvent", "Note"],
                [
                    [
                        str(row.run_order or ""),
                        row.well,
                        row.arm_id + (f" *({row.control})*" if row.control else ""),
                        *[row.levels.get(name, "") for name in factor_names],
                        _number(row.temperature_c),
                        _number(row.time_h),
                        row.solvent,
                        row.note,
                    ]
                    for row in rows
                ],
            ),
            "",
        ]

    if design.base.analytics:
        parts += [
            "## Analytics",
            "",
            *[
                f"- **{a.name}**"
                + (f" ({a.timing})" if a.timing else "")
                + (f" — {a.method}" if a.method else "")
                + (f" — measures {', '.join(a.measures)}" if a.measures else "")
                for a in design.base.analytics
            ],
            "",
        ]
    if design.base.in_process_controls:
        parts += [
            "## In-process controls",
            "",
            *[f"- {c}" for c in design.base.in_process_controls],
            "",
        ]
    if design.base.waste:
        # A `ProtocolBody` field with no reader anywhere: waste-disposal instructions were absent
        # from the bench document.
        parts += ["## Waste", "", design.base.waste, ""]
    if design.base.hazards:
        parts += [
            "## Hazards",
            "",
            "*Flags, not a clearance — this system screens and never certifies.*",
            "",
            *[f"- {h}" for h in design.base.hazards],
            "",
        ]

    expected = design.base.expected
    if expected.yield_percent is not None or expected.detail or expected.selectivity:
        detail = ", ".join(
            part
            for part in (
                f"{_number(expected.yield_percent)}% yield"
                if expected.yield_percent is not None
                else "",
                expected.selectivity,
                expected.detail,
            )
            if part
        )
        parts += ["## Expected", "", f"{detail} — *{expected.basis}*", ""]

    if design.evidence:
        parts += [
            "## Evidence",
            "",
            *[
                f"- **{ref.kind}**"
                + (f" `{ref.ref}`" if ref.ref else "")
                + (f" via `{ref.tool}`" if ref.tool else "")
                + f" — {ref.summary}"
                + (f" (supports {', '.join(ref.supports)})" if ref.supports else "")
                for ref in design.evidence
            ],
            "",
        ]

    if checks:
        failed = [c for c in checks if not c.passed]
        parts += ["## Checks", ""]
        parts += (
            [f"- **{c.severity}** `{c.check_id}` — {c.detail}" for c in failed]
            if failed
            else ["All checks passed."]
        )
        parts += [""]
    return "\n".join(parts).rstrip() + "\n"
