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

import re
from collections.abc import Callable
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.protocols.models import (
    DesignStatus,
    DesignSummary,
    ExperimentDesign,
    Factor,
    FactorLevel,
    ProtocolArm,
    ProtocolCheck,
    ProtocolStep,
    Setpoints,
    Well,
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
    # **The four an arm could override with nothing on the page saying so.** `## Conditions` renders
    # the *body* whenever there is more than one arm, and the run sheet carried only temperature,
    # time and solvent — so an arm overriding the atmosphere and the pressure rendered byte for byte
    # like one that did not. Measured: a design running arm A2 at 50 bar H2 printed a page saying
    # 1 bar N2, with `H2` and `50` appearing nowhere on it and no check firing. This is a document a
    # chemist runs from.
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
    # **Whether the checks below were graded against a procedure, which nothing on this payload
    # could say.** At the request stage the service reports every protocol-only check as a
    # *passing* note reading "not checked yet — this design holds only the ask", so a reader that
    # counts passes reports a clearance nobody issued. The browser guarded that on `status ==
    # "requested"`, which is a *proxy*: `advanced()` decides the status and `has_protocol` decides
    # the stage, independently — so a `draft` or `approved` design edited back down to the bare ask
    # keeps its status and got a green "15 checks passed" over a design with no charge table, no
    # procedure and no evidence. This is the value the stage was actually chosen by.
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

    model_config = ConfigDict(frozen=True, extra="forbid")


class DesignListing(BaseModel):
    """A page of designs."""

    designs: list[DesignSummary] = Field(default_factory=list)

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

#: The four that appear only when the arms disagree about them. They had no column at all, so an arm
#: overriding the atmosphere or the pressure rendered byte for byte like one that did not; giving
#: them a permanent column instead would bury the varying one among three constant columns on a
#: 96-row plate. What every arm shares is stated once under `## Conditions`.
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

    `## Conditions` used to render `design.base.setpoints` — what the body happens to hold, which
    is not what anybody runs the moment an arm overrides it. The run sheet is the other half and
    carries a column only when the arms *disagree*, so a field every arm overrode to the same value
    fell through both: measured on three arms all set to `N2` over a body reading `air`, the page
    said "Atmosphere: air", the run sheet had no atmosphere column, and the atmosphere the design is
    actually run under appeared nowhere on a document a chemist runs from.

    A field the arms disagree about comes back at its default, so the caller drops it and the run
    sheet's own rule shows it per row. That makes the two sections complementary by construction:
    every stated field is in exactly one of them, and neither list has to be kept in step with the
    other by hand.

    With no arms there is nothing to resolve and the body *is* the answer, which is the intake and
    the bodies-only protocol.
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
        # A design holding only the structured ask. Saying "0 arms over 0 factors" here would read
        # as an empty protocol rather than as an intake nobody has drafted yet. `has_protocol`
        # rather than the condition spelled out again: this was the fourth caller deciding it
        # separately, in the release whose own note says that is how the second and third got it
        # wrong.
        shape = "the structured ask, no procedure yet"
    elif design.is_single_experiment:
        # **The design, not the ask.** This branched on `request.mode`, so a 4-arm 2-factor plate
        # whose ask still said `single` summarised as "1 experiment". `== 1` rather than `<= 1`:
        # the first rewrite swallowed the zero-arm case too and described a body with no arms
        # declared as one experiment, which is a count nobody wrote.
        shape = "1 experiment"
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
    # **Every failed check that is not a blocker was called a "warning", and the count vanished the
    # moment a blocker existed.** A failed `note` is not a warning, and a design with one blocker
    # and four warnings reported only the blocker — on the one sentence `ProtocolReceipt.summary`
    # exists so a model can quote it without re-reading the design.
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


def _cell(text: str) -> str:
    """One table cell, escaped so free text cannot restructure the table.

    Cell contents are a chemist's own words — a level's rationale, a charge line's note, an arm's
    note — and none of them was escaped. A `|` overflowed the row, so GFM dropped the surplus cells
    and the text after the pipe vanished; a newline *terminated the table* and dumped the rest of
    the run sheet into the page as a paragraph. Both are reachable from any free-text field.
    """
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


#: Backtick runs, so a code span can be fenced longer than anything inside it.
_BACKTICKS = re.compile(r"`+")

#: Every character that opens a Markdown block when it starts a line. `` ` `` and `~` open a fenced
#: code block that swallows the rest of the document; `<` opens a raw HTML block; the other six are
#: headings, quotes, tables, lists and setext rules.
_BLOCK_OPENERS = frozenset("#>|-*+=~`<")

#: A leading ordered-list marker. CommonMark takes up to nine digits before the `.` or `)`.
_ORDERED_MARKER = re.compile(r"^(\d{1,9})([.)])")


def _code(value: str) -> str:
    """One identifier as an inline code span no run of backticks inside it can close.

    An `EvidenceRef.ref` is free text and a backtick in it closed the span early, so the rest of the
    citation rendered as prose. **A fixed doubled fence only moved the problem one backtick along**:
    CommonMark closes a span at the next run of *exactly* the opening length, so `` a``b `` written
    between two doubled fences closes on its own inner pair and renders `a` as code with `b` beside
    it as prose. The fence is therefore one longer than the longest run the value contains, and the
    padding spaces are what let the content itself begin or end with a backtick.
    """
    flat = " ".join(value.split())
    if "`" not in flat:
        return f"`{flat}`"
    fence = "`" * (max(len(run) for run in _BACKTICKS.findall(flat)) + 1)
    return f"{fence} {flat} {fence}"


def _text(value: str) -> str:
    r"""One piece of a chemist's free text, safe to place in the document's block flow.

    `_cell` keeps free text from restructuring a *table*; nothing protected the block context, and
    the fields outside tables are the same browser-supplied strings. Measured: a hazard line reading
    `## Waste\n\nQuench into water.` rendered a second `## Waste` section, so the page carried two
    waste headings with conflicting disposal instructions — one of them forged from a hazard string.
    A blank line inside a step ejected the rest of that step into an orphan paragraph between the
    numbered ones.

    Both come from the same two characters: a newline that ends the block, and a leading marker that
    starts a new one. Line breaks collapse to spaces (a bullet or a numbered step is one line by
    construction) and a leading block marker is escaped so it renders as itself. The text a chemist
    typed is preserved; only its power to open a section is not.

    **The first marker set was the ones a reader thinks of, and it left four openers out.** A
    leading `` ` `` or `~` opens a *fenced code block*, which swallows every following line of the
    document until it closes; a leading `<` opens a raw HTML block, which GFM renders; and a
    leading `1.` opens an ordered list. Each is a block opener on the same terms as `#`, and three
    of the four do more damage than the heading the set was written for.
    """
    flat = " ".join(value.split())
    if flat[:1] in _BLOCK_OPENERS:
        return f"\\{flat}"
    return _ORDERED_MARKER.sub(r"\1\\\2", flat, count=1)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    """A GitHub-flavoured Markdown table, or an empty string when there are no rows."""
    if not rows:
        return ""
    head = "| " + " | ".join(_cell(h) for h in headers) + " |"
    rule = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(_cell(cell) or "—" for cell in row) + " |" for row in rows]
    return "\n".join([head, rule, *body])


def _number(value: float | None) -> str:
    """One number as a chemist reads it, and never in exponent form inside laboratory range.

    `%g` alone turns a kilogram-scale charge into `1.23457e+06` mg. `.10g`, which replaced it,
    fixed that and bought false precision everywhere else — a 1/6 M concentration rendered
    `0.1666666667 M` and a 200/3 yield `66.66666667%`, ten significant figures off a balance that
    reads four, on the document a chemist runs from. A whole number is printed whole (no exponent
    below 1e15, which is past any laboratory quantity).

    **Six significant figures is what `%.6g` gives and is not what this function gives above 1e6**,
    and the docstring claimed otherwise for as long as the branch below existed. `1234567.8` comes
    back as `'1234567.8'` — eight figures — because inside `[1e-4, 1e15)` the number is written out
    positionally rather than in exponent form. That is deliberate rather than an oversight: `%.6g`
    prints both 999999.5 and 1000000.5 as `1e+06`, and those are two different weigh-outs on a
    document somebody weighs from. Six figures is the ceiling below 1e6, where `%.6g` has decimals
    to spend on it; above 1e6 the integer part is already six figures and trimming further would
    collide.
    """
    if value is None:
        return ""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    text = f"{value:.6g}"
    if "e" not in text and "E" not in text:
        return text
    # **`%g`'s exponent is unreadable on a bench sheet, and it collides.** The docstring's own
    # example still reproduced: a kilogram-scale charge printed `1.23457e+06` mg, and — worse —
    # 999999.5 and 1000000.5 mg both printed `1e+06`, two different weigh-outs shown as one number
    # on a document a chemist weighs from. Inside the range a laboratory quantity actually occupies,
    # print it out; outside it an exponent is the honest form (1e-05 mmol is how that is written).
    if 1e-4 <= abs(value) < 1e15:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return text


def _common_unit(factor: Factor) -> str:
    """The unit every level of this factor agrees on, when the factor itself declares none."""
    units = {level.unit for level in factor.levels if level.unit}
    return units.pop() if len(units) == 1 else ""


def _level(level: FactorLevel) -> str:
    """One level as the Factors table shows it — its label, its value, and the value's own unit.

    `FactorLevel.unit` was dropped entirely while the `Unit` column showed `Factor.unit`, so levels
    of `0` and `100` °C under a factor declaring no unit rendered as bare numbers beside a column
    truthfully reporting "no unit" — which is exactly the bare-number-reads-as-an-equivalent failure
    that column was added to prevent.
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
    """The design as a document a chemist reads — the form a report or a note body carries.

    Deliberately not what a tool returns (see the module docstring): this is for a human reader and
    for anything that renders one, and it is lossy in the direction a document should be — it shows
    the shared body once and the arms as a table, rather than N protocols.
    """
    request = design.request
    # **Every one of these is browser-supplied free text and none of them was escaped.** Measured,
    # a title reading "T\n\n## Forged" put a second `## Forged` section on the page and a goal did
    # it again — `_text` was applied to the steps, the hazards and the waste and to nothing above
    # them, which is the half of the document a chemist reads first.
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

    # **The conditions the arms actually run at, not the ones the body happens to hold.**
    # `## Conditions` rendered `base.setpoints` unconditionally and the run sheet carries a column
    # only when the arms disagree, so a value every arm overrode to the *same* thing appeared in
    # neither place while the body's own value printed as fact. Measured on three arms all set to
    # N2 over a body reading `air`: the page said "Atmosphere: air", the run sheet had no
    # atmosphere column, and nothing anywhere named the atmosphere the design is run under.
    #
    # The one-arm case was the first half of this and is now the same rule: a single experiment
    # whose arm overrode the body got neither the body's value nor a run sheet, so the document
    # said 80 °C / 16 h / dioxane for an arm the design runs at 120 °C / 2 h / toluene.
    #
    # So this section shows what every arm agrees on, resolved; a field they disagree about is
    # dropped here and the run sheet's own rule picks it up. The two are complementary by
    # construction rather than by two lists somebody keeps in step.
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
        # A reader must not take this list for the whole of the conditions when it is not. Said
        # only when a field was actually dropped, so the ordinary page carries no caveat about a
        # case it is not in.
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
    # **Every design with arms gets a run sheet, and this comment used to say so over an unchanged
    # gate.** The gate really was left as it was, so a single-arm design still had no run sheet —
    # and for a lone *negative control* the words `control` and `negative` then appeared nowhere on
    # the page. `if rows:` is what the sentence claimed.
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
        # **Failed checks, plus every `note`.** Listing failures only is what made a finding
        # invisible, and flipping four checks to `_fail` was the wrong half of that fix: a note is
        # advisory content rather than a verdict, so `coverage_is_stated`'s sentence about what a
        # reduced design confounds belongs on the page whether or not it "failed". That check is a
        # passing note again, and a correct fractional plate no longer reports a failure it cannot
        # clear.
        failed = [c for c in checks if not c.passed or c.severity == "note"]
        parts += ["## Checks", ""]
        parts += (
            [f"- **{c.severity}** `{c.check_id}` — {c.detail}" for c in failed]
            if failed
            else ["All checks passed."]
        )
        parts += [""]
    return "\n".join(parts).rstrip() + "\n"
