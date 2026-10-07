"""What a design looks like to its three readers: a model, a browser and a chemist.

One JSON payload serves the model and the browser, so the two can never disagree (the front end
parses JSON and renders nothing on failure). The receipt is deliberately not the whole design: a
model that just authored a protocol needs the id, revision and check results; the document is
one `read_experiment_protocol` (or `GET /protocols/{id}`) away. `render_markdown` is the
third reader, a chemist reading a document, and is never what a tool returns.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.markdown import render_table
from chemclaw.protocols.models import (
    DesignStatus,
    ExperimentDesign,
    Factor,
    FactorLevel,
    ProtocolArm,
    ProtocolCheck,
    ProtocolStep,
    Setpoints,
    Well,
)

#: How many arms a receipt lists before summarising the rest; the full arm table is the largest
#: thing in the payload and not what a model needs back from its own write.
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
    # The overridable setpoints beyond temperature, time and solvent, so an arm's atmosphere or
    # pressure override is visible on the run sheet.
    atmosphere: str = ""
    pressure_bar: float | None = None
    concentration_molar: float | None = None
    ph: float | None = None
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
    # Whether the checks were graded against a procedure. At the request stage protocol-only checks
    # are passing "not checked yet" notes, and status is not a reliable proxy for the stage.
    has_protocol: bool = False
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
    # The run sheet's path (`export.run_sheet_path`), not the CSV itself, so a model never carries a
    # second copy of the plate to retype. Filled by the caller because `export` imports this module.
    run_sheet: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid")


class _Column(NamedTuple):
    """One run-sheet condition column: its heading, and how to read it off a row."""

    heading: str
    value: Callable[[ArmRow], str]


#: The three a bench sheet always carries, whether or not they vary: a chemist setting up a run
#: reads the temperature, the time and the solvent off the row in front of them.
_RUN_SHEET_ALWAYS: tuple[_Column, ...] = (
    _Column("T /°C", lambda row: _number(row.temperature_c)),
    _Column("t /h", lambda row: _number(row.time_h)),
    _Column("Solvent", lambda row: row.solvent),
)

#: Columns that appear only when the arms disagree about them, so a varying one is not buried among
#: constant columns. What every arm shares is stated once under `## Conditions`.
_RUN_SHEET_WHEN_VARYING: tuple[_Column, ...] = (
    _Column("c /M", lambda row: _number(row.concentration_molar)),
    _Column("Atmosphere", lambda row: row.atmosphere),
    _Column("p /bar", lambda row: _number(row.pressure_bar)),
    _Column("pH", lambda row: _number(row.ph)),
)


def _arm_row(design: ExperimentDesign, arm: ProtocolArm, wells: dict[str, Well]) -> ArmRow:
    """One arm as a row, with its conditions resolved against the shared body."""
    well = wells.get(arm.arm_id)
    points = design.setpoints_for(arm)
    return ArmRow(
        arm_id=arm.arm_id,
        well=well.label if well else "",
        run_order=well.run_order if well else 0,
        levels=dict(arm.levels),
        temperature_c=points.temperature_c,
        time_h=points.time_h,
        solvent=points.solvent,
        atmosphere=points.atmosphere,
        pressure_bar=points.pressure_bar,
        concentration_molar=points.concentration_molar,
        ph=points.ph,
        control=arm.control,
        replicate_of=arm.replicate_of,
        note=arm.note,
    )


def shared_setpoints(design: ExperimentDesign) -> Setpoints:
    """The conditions **every arm agrees on**, each arm resolved against the shared body first.

    A field the arms disagree about comes back at its default, so the caller drops it and the run
    sheet shows it per row: every stated field appears in exactly one of the two sections by
    construction. With no arms the body is the answer.
    """
    if not design.arms:
        return design.base.setpoints
    resolved = [design.setpoints_for(arm) for arm in design.arms]
    first, rest = resolved[0], resolved[1:]
    return Setpoints.model_validate(
        {
            field: value
            for field, value in first
            if all(getattr(other, field) == value for other in rest)
        }
    )


def run_sheet_rows(design: ExperimentDesign) -> list[ArmRow]:
    """Every arm as a row, in run order when there is a layout and in arm order otherwise."""
    wells = {well.arm_id: well for well in (design.layout.wells if design.layout else [])}
    rows = [_arm_row(design, arm, wells) for arm in design.arms]
    # A randomised design's whole point is that it is *run* in an order the plate does not show, so
    # the run sheet is sorted by that order and the plate map is what shows position.
    return sorted(rows, key=lambda r: (r.run_order == 0, r.run_order)) if wells else rows


def summarise(design: ExperimentDesign, checks: list[ProtocolCheck]) -> str:
    """The one sentence that says what this design is and whether it is runnable."""
    failed = [c for c in checks if not c.passed]
    blocking = [c for c in failed if c.severity == "blocker"]
    if not design.has_protocol:
        # A design holding only the structured ask: an intake, not an empty protocol.
        shape = "the structured ask, no procedure yet"
    elif design.is_single_experiment:
        # The design's shape, not the ask's mode. A body with no arms declared is not "1
        # experiment":
        # `is_single_experiment` is `<= 1` for the check exemptions it drives, but here a count is
        # being
        # reported.
        shape = "1 experiment" if design.distinct_arms else "a procedure with no arms declared"
        # The runs are not lost with the word: a triplicate is one experiment and three arms, and
        # a summary saying only "1 experiment" would hide two of them.
        if len(design.arms) > 1:
            shape += f", {len(design.arms)} runs"
    else:
        controls = sum(1 for a in design.arms if a.control)
        shape = f"{len(design.arms)} arms over {len(design.factors)} factors"
        if controls:
            shape += f" plus {controls} control(s)"
        if design.layout:
            shape += f" on a {design.layout.plate_format}-well plate"
    # Warnings are counted separately from blockers (and failed notes are not warnings), so the
    # summary a model quotes reports both.
    warnings = [c for c in failed if c.severity == "warning"]
    notes = [c for c in failed if c.severity == "note"]
    counts = [
        f"{len(blocking)} blocking check(s)" if blocking else "no blocking checks",
        f"{len(warnings)} warning(s)" if warnings else "",
        f"{len(notes)} note(s)" if notes else "",
    ]
    verdict = ", ".join(part for part in counts if part)
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
        has_protocol=design.has_protocol,
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


#: Backtick runs, so a code span can be fenced longer than anything inside it.
_BACKTICKS = re.compile(r"`+")

#: Every character that opens a Markdown block at the start of a line: headings, quotes, tables,
#: lists, setext rules, fenced code (`` ` `` and `~`) and raw HTML (`<`).
_BLOCK_OPENERS = frozenset("#>|-*+=~`<")

#: A leading ordered-list marker. CommonMark takes up to nine digits before the `.` or `)`.
_ORDERED_MARKER = re.compile(r"^(\d{1,9})([.)])")


def _code(value: str) -> str:
    """One identifier as an inline code span no run of backticks inside it can close.

    CommonMark closes a span at the next run of exactly the opening length, so the fence is one
    longer than the longest run in the value, with padding spaces so content may begin or end with a
    backtick.
    """
    flat = " ".join(value.split())
    if "`" not in flat:
        return f"`{flat}`"
    fence = "`" * (max(len(run) for run in _BACKTICKS.findall(flat)) + 1)
    return f"{fence} {flat} {fence}"


def _text(value: str) -> str:
    r"""One piece of a chemist's free text, safe to place in the document's block flow.

    Browser-supplied text could otherwise forge sections (a hazard reading `## Waste`) or split a
    step. Line breaks collapse to spaces and a leading block marker (`_BLOCK_OPENERS` or an ordered
    list `1.`) is escaped. The typed text is preserved; only its power to open a block is not.
    """
    flat = " ".join(value.split())
    if flat[:1] in _BLOCK_OPENERS:
        return f"\\{flat}"
    return _ORDERED_MARKER.sub(r"\1\\\2", flat, count=1)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    """A GitHub-flavoured Markdown table, or an empty string when there are no rows.

    Grid and cell escaping are `core.markdown`'s. The zero-row rule is this document's own: a run
    sheet with no charge lines has no charge table.
    """
    if not rows:
        return ""
    return render_table(headers, rows)


def _number(value: float | None) -> str:
    """One number as a chemist reads it, and never in exponent form inside laboratory range.

    Six significant figures below 1e6; inside `[1e-4, 1e15)` larger numbers are written out
    positionally so distinct weigh-outs (999999.5 vs 1000000.5) never collapse to one `1e+06`.
    Whole numbers print whole. Outside that range an exponent is the honest form.
    """
    if value is None:
        return ""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    text = f"{value:.6g}"
    if "e" not in text and "E" not in text:
        return text
    # Positional inside laboratory range: `%g`'s exponent is unreadable on a bench sheet and
    # collides neighbouring values.
    if 1e-4 <= abs(value) < 1e15:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return text


def _common_unit(factor: Factor) -> str:
    """The unit every level of this factor agrees on, when the factor itself declares none."""
    units = {level.unit for level in factor.levels if level.unit}
    return units.pop() if len(units) == 1 else ""


def _level(level: FactorLevel) -> str:
    """One level as the Factors table shows it: its label, its value, and the value's own unit.

    The level's unit is shown because the column shows only `Factor.unit`; a bare number would read
    as an equivalent.
    """
    if level.value is None:
        return level.label
    unit = f" {level.unit}" if level.unit else ""
    return f"{level.label} ({_number(level.value)}{unit})"


def _step_conditions(step: ProtocolStep) -> str:
    """A step's own temperature and duration, appended to its line when it states them."""
    stated = [
        f"{_number(step.temperature_c)} °C" if step.temperature_c is not None else "",
        f"{_number(step.duration_h)} h" if step.duration_h is not None else "",
    ]
    said = [part for part in stated if part]
    return f" — {', '.join(said)}" if said else ""


def render_markdown(design: ExperimentDesign, checks: list[ProtocolCheck] | None = None) -> str:
    """The design as a document a chemist reads: the form a report or a note body carries.

    Not what a tool returns. Lossy in the right direction: the shared body once, the arms as a
    table.
    """
    request = design.request
    # Title and goal are browser-supplied free text too, so they go through `_text`.
    parts: list[str] = [f"# {_text(request.title)}", "", f"**Goal.** {_text(request.goal)}", ""]
    if request.reaction_smiles:
        # `_code`, not a bare span: a backtick in a SMILES closes the span and spills the rest.
        parts += [f"**Transformation.** {_code(request.reaction_smiles)}", ""]
    if request.objectives:
        parts += ["**Objectives.** " + ", ".join(_text(o) for o in request.objectives), ""]
    if request.forbidden:
        # The chemist's hard exclusions, whose violation is a blocker — and which the document a
        # chemist reads did not mention.
        parts += ["**Ruled out.** " + ", ".join(_text(f) for f in request.forbidden), ""]

    # The conditions the arms actually run at (`shared_setpoints`), not what the body holds: a value
    # every arm overrides identically would otherwise appear nowhere, and a single arm's override
    # would
    # be misreported. Fields the arms disagree about are left to the run sheet.
    solo = design.arms[0] if len(design.arms) == 1 else None
    points = shared_setpoints(design)
    stated = [
        (label, value)
        for label, value in (
            (
                "Temperature",
                f"{_number(points.temperature_c)} °C" if points.temperature_c is not None else "",
            ),
            ("Time", f"{_number(points.time_h)} h" if points.time_h is not None else ""),
            ("Solvent", _text(points.solvent)),
            (
                "Concentration",
                f"{_number(points.concentration_molar)} M"
                if points.concentration_molar is not None
                else "",
            ),
            ("Atmosphere", _text(points.atmosphere)),
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
        # Said only when a field was dropped, so this list is never mistaken for all the conditions.
        if any(design.setpoints_for(arm) != points for arm in design.arms):
            parts += ["*The conditions every arm shares; the run sheet carries what varies.*", ""]
        if solo is not None and solo.note:
            parts += [f"*{_text(solo.note)}*", ""]

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
            f"{step.index}. *({step.kind})* {_text(step.text)}" + _step_conditions(step)
            for step in design.base.steps
        ]
        parts += [""]

    if design.factors:
        parts += [
            "## Factors",
            "",
            _table(
                # `Unit` keeps a bare number from reading as an equivalent; the per-level rationale
                # is the most
                # useful sentence on a screening plate.
                ["Factor", "Kind", "Role", "Unit", "Levels", "Why"],
                [
                    [
                        f.name,
                        f.kind,
                        str(f.role),
                        f.unit or _common_unit(f),
                        ", ".join(_level(level) for level in f.levels),
                        "; ".join(level.rationale for level in f.levels if level.rationale),
                    ]
                    for f in design.factors
                ],
            ),
            "",
        ]

    rows = run_sheet_rows(design)
    # Every design with arms gets a run sheet, including a single arm (so a lone control is named).
    if rows:
        factor_names = [f.name for f in design.factors]
        # Always the three a bench sheet carries, plus any of the other four the arms disagree
        # about — see the two constants for what that closed.
        conditions = [
            *_RUN_SHEET_ALWAYS,
            *[
                column
                for column in _RUN_SHEET_WHEN_VARYING
                if len({column.value(row) for row in rows}) > 1
            ],
        ]
        parts += [
            "## Run sheet",
            "",
            _table(
                ["Run", "Well", "Arm", *factor_names, *[c.heading for c in conditions], "Note"],
                [
                    [
                        str(row.run_order or ""),
                        row.well,
                        row.arm_id
                        + (f" *({row.control})*" if row.control else "")
                        + (f" *(replicate of {row.replicate_of})*" if row.replicate_of else ""),
                        *[row.levels.get(name, "") for name in factor_names],
                        *[column.value(row) for column in conditions],
                        row.note,
                    ]
                    for row in rows
                ],
            ),
            "",
        ]
        if design.layout and design.layout.randomized:
            # The run order is a shuffle, and a sheet that does not say so reads as the plate's own
            # order — with nothing recording that it is reproducible.
            parts += [
                f"*Run order randomised, seed {design.layout.seed}. "
                "Run in the order given, not in plate order.*",
                "",
            ]

    if design.base.analytics:
        parts += [
            "## Analytics",
            "",
            *[
                f"- **{_text(a.name)}**"
                + (f" ({_text(a.timing)})" if a.timing else "")
                + (f" — {_text(a.method)}" if a.method else "")
                + (f" — measures {', '.join(_text(m) for m in a.measures)}" if a.measures else "")
                for a in design.base.analytics
            ],
            "",
        ]
    if design.base.in_process_controls:
        parts += [
            "## In-process controls",
            "",
            *[f"- {_text(c)}" for c in design.base.in_process_controls],
            "",
        ]
    if design.base.waste.strip():
        # A `ProtocolBody` field with no reader anywhere: waste-disposal instructions were absent
        # from the bench document.
        parts += ["## Waste", "", _text(design.base.waste), ""]
    if design.base.hazards:
        parts += [
            "## Hazards",
            "",
            "*Flags, not a clearance — this system screens and never certifies.*",
            "",
            *[f"- {_text(h)}" for h in design.base.hazards],
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
        parts += ["## Expected", "", f"{_text(detail)} — *{expected.basis}*", ""]

    if design.evidence:
        parts += [
            "## Evidence",
            "",
            *[
                f"- **{ref.kind}**"
                # A backtick inside the id closes the code span it is written into, so the rest of
                # the reference renders as prose — `_code` keeps the span whole.
                + (f" {_code(ref.ref)}" if ref.ref else "")
                + (f" via {_code(ref.tool)}" if ref.tool else "")
                + f" — {_text(ref.summary)}"
                + (
                    f" (supports {', '.join(_text(sup) for sup in ref.supports)})"
                    if ref.supports
                    else ""
                )
                for ref in design.evidence
            ],
            "",
        ]

    if checks:
        # Failed checks plus every `note`: a note is advisory content (such as what a reduced design
        # confounds), shown whether or not it failed.
        failed = [c for c in checks if not c.passed or c.severity == "note"]
        parts += ["## Checks", ""]
        parts += (
            [f"- **{c.severity}** `{c.check_id}` — {c.detail}" for c in failed]
            if failed
            else ["All checks passed."]
        )
        parts += [""]
    return "\n".join(parts).rstrip() + "\n"
