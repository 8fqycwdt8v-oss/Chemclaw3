"""Map a canonical ORD reaction to an ELN transcription record.

The pure mapping from an `OrdReaction` to the stored `ReactionRecord`: reaction SMILES, headline
conditions (scale first), the charge sheet, the impurity profile and the full procedure, so a
chemist reaching the record from a structure search gets the recipe.

Nothing here infers anything (D-2026-08-25-an-eln-transcription-is-data-not-a-claim): every field is
read from the entry or rendered from fields that were, so the result is data, not a knowledge claim.
That holds only if adapters also infer nothing (see `json_adapter._number`). The body carries no
`[[wikilink]]`, enforced by `_without_wikilinks`, since the source's free text reaches it verbatim.
"""

import re
from typing import Literal, assert_never

from chemclaw.ingest.eln.ord import (
    Component,
    Impurity,
    OrdReaction,
    OutcomeClass,
    ReactionStep,
    RecordTier,
    Role,
    UnstructuredComponent,
)
from chemclaw.ingest.eln.records import ReactionRecord
from chemclaw.kg.note import ProcessConditions

# Each `[` that another `[` follows; a lookahead, so the substitution cannot manufacture the
# delimiter it removes (see `_without_wikilinks`).
_OPENING_BRACKET_PAIR = re.compile(r"\[(?=\[)")


def _without_wikilinks(body: str) -> str:
    """Neutralize any `[[rel:id]]` span the source's own free text spelled.

    `kg.note` parses rendered bodies for links, so source text like `[[contradicts:reaction-1234]]`
    would forge a real relation; with no review step, this function is the control. Applied once to
    the assembled body so every field, present and future, is covered.

    The substitution is visible and lossless, so a reader sees what the source wrote. A
    per-character lookahead rather than `str.replace("[[", "[ [")`, which turns `[[[x]]` into `[
    [[x]]` and leaves a valid delimiter.
    """
    return _OPENING_BRACKET_PAIR.sub("[ ", body)


def record_from_ord_reaction(reaction: OrdReaction) -> ReactionRecord:
    """Map an `OrdReaction` to the transcription record the corpus stores (idempotent id)."""
    body = _without_wikilinks(
        f"{_lead(reaction)}"
        f"{_hypothesis_block(reaction)}"
        f"{_conditions_block(reaction)}"
        f"{_charge_block(reaction)}"
        f"{_species_block(reaction)}"
        f"{_impurity_block(reaction)}"
        f"{_procedure_block(reaction)}"
        f"{_attribute_block(reaction)}"
    )
    return ReactionRecord(
        # The ELN's own id, unprefixed. `reaction-<id>` is the *citation* spelling
        # (`kg.note.note_id_for_reaction`) and belongs to whoever cites this, not to the row.
        reaction_id=reaction.reaction_id,
        source=reaction.provenance,
        compound_smiles=_principal_product(reaction),
        # The project is the one grouping key the entry carries and what `tag=` filters on. Nothing
        # derived (scale band, outcome word), which would be a taxonomy no chemist agreed to.
        project=reaction.project or None,
        # The experiment's own date makes the record time-scopable (`since`/`until`). A result has
        # no expiry; it is superseded by a human claim in a note.
        performed_at=reaction.performed_at,
        # The numbers a chemist compares, kept as numbers on the existing reaction row
        # (`ProcessConditions`), so comparisons need not re-parse the body's prose.
        conditions=_conditions(reaction),
        # Each compared role's structures, so turn-time comparison diffs the same sets as the
        # campaign note (`memory.progression.changes_between`); amounts and order stay in the body
        # (`RoleSpecies`). None on a citation-only record, whose named species would otherwise read
        # as removed.
        species=reaction.role_species() if reaction.tier is RecordTier.STRUCTURED else None,
        tier=reaction.tier,
        # Last: pydantic truncates a long `input_value` repr in the middle, and an unstorable byte
        # in the body is reported from its tail.
        body=body,
    )


def _lead(reaction: OrdReaction) -> str:
    """The body's first line: the reaction SMILES, or why a citation-only record has none.

    Stated in the body because every reader gets the body; it says the source named species without
    structures and that structure search does not apply, never what the structure might be.
    """
    if reaction.tier is RecordTier.STRUCTURED:
        return f"Reaction `{reaction.reaction_smiles()}` from ELN entry {reaction.reaction_id}.\n\n"
    count = len(reaction.unstructured)
    return (
        f"Reaction from ELN entry {reaction.reaction_id}. Structure not given by the source for "
        f"{count} species ({'it is' if count == 1 else 'they are'} named under Species exactly as "
        "the source gave them), so this record is citation-only: cite it for what it states, and "
        "expect no structure or similarity search to find it.\n\n"
    )


def _stated_outcome(
    outcome: OutcomeClass | None,
) -> Literal["success", "failure", "inconclusive"] | None:
    """The frontmatter spelling of an outcome a source stated, or `None` when it stated none.

    A `match` with `assert_never` so mypy enforces that every `OutcomeClass` member gets a spelling;
    a dict would fail with `KeyError` at runtime and abort the whole sync. A stated success is
    spelled; silence is `None` (D-2026-08-26-silence-is-not-a-successful-run).
    """
    match outcome:
        case None:
            return None
        case OutcomeClass.SUCCESS:
            return "success"
        case OutcomeClass.FAILURE:
            return "failure"
        case OutcomeClass.INCONCLUSIVE:
            return "inconclusive"
        case _:
            assert_never(outcome)


def _conditions(reaction: OrdReaction) -> ProcessConditions | None:
    """The run's setpoints and outcomes as frontmatter, or `None` when it recorded none of them.

    `None` rather than an empty block, which would claim the question was asked and answered
    emptily. `outcome` is `None` when the source stated none.
    """
    impurity = reaction.major_impurity()
    conditions = ProcessConditions(
        temperature_c=reaction.temperature_c,
        time_h=reaction.time_h,
        yield_percent=reaction.yield_percent,
        purity_percent=reaction.purity_percent,
        outcome=_stated_outcome(reaction.outcome_class),
        major_impurity=(impurity.name or impurity.smiles) if impurity else None,
        impurity_area_percent=impurity.area_percent if impurity else None,
    )
    # `is not None`, never truthiness: a 0 °C bath, a 0% yield or a 0 h hold is a recorded value,
    # and absent means "not recorded", never zero.
    return conditions if any(value is not None for value in dict(conditions).values()) else None


def _principal_product(reaction: OrdReaction) -> str | None:
    """The molecule this record is *about*, when the entry names exactly one product.

    The column by-compound questions and playbook grouping join on. Only with one product: picking
    among several would file the run under a compound the chemist did not mean, and a wrong
    `compound_smiles` is worse than none.
    """
    if reaction.product_count() != 1 or not reaction.outcomes:
        return None
    return reaction.outcomes[0].smiles


def _hypothesis_block(reaction: OrdReaction) -> str:
    """Lead with what the run was testing, when the source recorded it (D-162).

    First because it is the question the conditions answer. Empty when unrecorded; the body never
    says "no hypothesis".
    """
    if not reaction.hypothesis:
        return ""
    return f"Tested: {' '.join(reaction.hypothesis.split())}\n\n"


def _measured(value: float) -> str:
    """Render a number this system *computed*, without the binary tail of its own arithmetic.

    Yield and purity are echoed verbatim (the source's own digits); converted values (K->°C,
    minutes->hours, g->mg) carry float artefacts, so `195.15 - 273.15` renders as -78 rather than
    -77.99999999999997. Twelve significant figures: enough for any balance at kilo scale, short of
    float noise. Stored `conditions` keep the full double; this is only the body.
    """
    return f"{value:.12g}"


def _conditions_block(reaction: OrdReaction) -> str:
    """Render the headline conditions (scale/temperature/time/yield) as a bullet list."""
    conditions = []
    if (scale := _scale(reaction)) is not None:
        conditions.append(f"scale: {scale}")
    if reaction.temperature_c is not None:
        conditions.append(f"temperature: {_measured(reaction.temperature_c)} °C")
    if reaction.time_h is not None:
        conditions.append(f"time: {_measured(reaction.time_h)} h")
    if reaction.yield_percent is not None:
        conditions.append(f"yield: {reaction.yield_percent}%")
    if reaction.purity_percent is not None:
        conditions.append(f"purity: {reaction.purity_percent}%")
    if reaction.performed_at is not None:
        conditions.append(f"performed: {reaction.performed_at.isoformat()}")
    if reaction.outcome_class in (OutcomeClass.FAILURE, OutcomeClass.INCONCLUSIVE):
        # A failure or inconclusive outcome is stated in the body so "did not work" is distinct from
        # "no number recorded". A stated success is not written here (the frontmatter carries it),
        # and an unstated outcome writes nothing.
        outcome = f"outcome: {reaction.outcome_class.value}"
        if reaction.failure_reason:
            outcome += f" — {reaction.failure_reason}"
        conditions.append(outcome)
    return "".join(f"- {c}\n" for c in conditions)


def _scale(reaction: OrdReaction) -> str | None:
    """The run's scale, as one bullet at the *top* of the conditions.

    Scale is the context every condition is read against. Reactants only: solvent tracks the vessel
    and reagents (excess base) would inflate the figure.

    Mass, then `amount_mmol`, then stated volume, each unit-labelled; chosen per reactant, so a
    record mixing forms reports every form rather than under-reporting (which makes a pilot read as
    a bench run). `None` when nothing is charged.

    First in the block because retrieval excerpts are a character prefix of the body; not a tag,
    which would need invented bands.
    """
    # A named-only reactant was charged too, and its amount counts toward the scale the same way.
    reactants = [c for c in (*reaction.inputs, *reaction.unstructured) if c.role is Role.REACTANT]
    masses = [c.mass_mg for c in reactants if c.mass_mg is not None]
    # Only those with no mass, so a reactant carrying both is counted once, on the preferred form.
    amounts = [c.amount_mmol for c in reactants if c.mass_mg is None and c.amount_mmol is not None]
    # Volumes only for reactants with neither mass nor moles: without a density a volume is a third
    # labelled term ("40 g + 10 mL"), not a conversion.
    volumes = [
        c.volume_ml
        for c in reactants
        if c.mass_mg is None and c.amount_mmol is None and c.volume_ml is not None
    ]
    grams = f"{_measured(sum(masses) / 1000)} g" if masses else ""
    mmol = f"{_measured(sum(amounts))} mmol" if amounts else ""
    millilitres = f"{_measured(sum(volumes))} mL" if volumes else ""
    charged = " + ".join(part for part in (grams, mmol, millilitres) if part)
    if not charged:
        return None
    return f"{charged} of reactants charged"


def _charge_block(reaction: OrdReaction) -> str:
    """Render what was actually charged, per input — the detail behind the one-line scale.

    Prose for a reader, so the species carrying the mass is visible rather than taken on trust;
    nothing parses it, and a per-species amount column would be a schema decision for a consumer
    that does not exist yet. Empty when no input carries an amount or attribute. Once present, every
    input is listed, since omission would read as "not charged". Empty for a citation-only record,
    whose `## Species` block lists everything.
    """
    if reaction.tier is RecordTier.CITATION_ONLY:
        return ""
    # Attributes count too, so a species with only an `amount_unmeasured` or lot number still gets
    # its row; only a record with nothing per species gets no section.
    if not any(
        c.mass_mg is not None
        or c.amount_mmol is not None
        or c.volume_ml is not None
        or c.attributes
        for c in reaction.inputs
    ):
        return ""
    lines = "".join(f"- {_charge_line(c)}\n" for c in reaction.inputs)
    return f"\n## Charge\n\n{lines}"


def _charge_line(component: Component) -> str:
    """One charged species: its structure, its role, and whatever amount was recorded."""
    amounts = _amounts(component)
    detail = ", ".join(amounts) if amounts else "amount not recorded"
    line = f"`{component.smiles}` ({component.role.value}): {detail}"
    # Per-species attributes stay on their row: a lot number describes this charge, and two lots of
    # one reagent must stay distinguishable.
    if component.attributes:
        line += " — " + _attribute_text(component.attributes)
    return line


def _species_block(reaction: OrdReaction) -> str:
    """Every species of a citation-only record, drawn or named — what its missing SMILES would say.

    Empty for a structured record. Inputs before products; a named-only species is followed by
    "structure not given by the source".
    """
    if reaction.tier is RecordTier.STRUCTURED:
        return ""
    named = reaction.unstructured
    species: list[Component | UnstructuredComponent] = [
        *reaction.inputs,
        *(c for c in named if c.role is not Role.PRODUCT),
        *reaction.outcomes,
        *(c for c in named if c.role is Role.PRODUCT),
    ]
    lines = "".join(f"- {_species_line(c)}\n" for c in species)
    return f"\n## Species\n\n{lines}"


def _species_line(species: Component | UnstructuredComponent) -> str:
    """One species of a citation-only record: its identity, its role, and any recorded amount."""
    if isinstance(species, Component):
        identity = f"`{species.smiles}`"
    else:
        identity = f"{species.name} — structure not given by the source"
    line = f"{identity} ({species.role.value})"
    if amounts := _amounts(species):
        line += f": {', '.join(amounts)}"
    if species.attributes:
        line += " — " + _attribute_text(species.attributes)
    return line


def _amounts(component: Component | UnstructuredComponent) -> list[str]:
    """The amounts the source recorded for one species, each unit-labelled."""
    amounts = []
    if component.mass_mg is not None:
        amounts.append(f"{_measured(component.mass_mg)} mg")
    if component.amount_mmol is not None:
        amounts.append(f"{_measured(component.amount_mmol)} mmol")
    # Volume is a recorded amount; "amount not recorded" would be false for a volumetric charge
    # (`Component.volume_ml`).
    if component.volume_ml is not None:
        amounts.append(f"{_measured(component.volume_ml)} mL")
    return amounts


def _attribute_text(attributes: dict[str, str]) -> str:
    """Render an attribute bag as `key: value` pairs, in the order the binding produced them."""
    return ", ".join(f"{key}: {value}" for key, value in attributes.items())


def _impurity_block(reaction: OrdReaction) -> str:
    """Render the impurity profile, the half of the outcome yield alone never captures.

    In the body, not only frontmatter, because retrieval reads bodies.
    """
    if not reaction.impurities:
        return ""
    lines = "".join(f"- {_impurity_line(imp)}\n" for imp in reaction.impurities)
    return f"\n## Impurities\n\n{lines}"


def _impurity_line(impurity: Impurity) -> str:
    """One impurity: whatever the source actually recorded, never a fabricated identity."""
    label = impurity.name or impurity.smiles or "unidentified"
    detail = []
    if impurity.smiles and impurity.name:
        detail.append(f"`{impurity.smiles}`")
    if impurity.area_percent is not None:
        detail.append(f"{impurity.area_percent}% area")
    return f"{label} ({', '.join(detail)})" if detail else label


def _procedure_block(reaction: OrdReaction) -> str:
    """Render the recipe: the ordered steps, the prose the source recorded, or both.

    Warehouse sources record the protocol only as `procedure_text`, so prose is rendered when there
    are no steps. When both exist, they are compared by containment: `json_adapter` steps are cuts
    of the prose and stand alone, while `ord_adapter` steps come from structured fields and may omit
    what the chemist's prose says, so both are rendered. Containment needs no tuned threshold.
    """
    prose = " ".join((reaction.procedure_text or "").split())
    if not reaction.steps:
        return f"\n## Procedure\n\n{prose}\n" if prose else ""
    lines = "".join(f"{step.index}. {_step_line(step)}\n" for step in reaction.steps)
    block = f"\n## Procedure\n\n{lines}"
    if prose and not _steps_segment(reaction, prose):
        block += f"\n### Procedure as recorded\n\n{prose}\n"
    return block


def _steps_segment(reaction: OrdReaction, prose: str) -> bool:
    """Whether these steps are a segmentation of `prose` rather than an independent account.

    True when every step's text appears verbatim in the whitespace-normalized prose.
    """
    return all(" ".join(step.text.split()) in prose for step in reaction.steps)


def _step_line(step: ReactionStep) -> str:
    """One procedure line: the instruction, tagged with its kind and any parsed conditions."""
    detail = [f"_{step.kind.value}_"]
    if step.temperature_c is not None:
        detail.append(f"{step.temperature_c} °C")
    if step.duration_h is not None:
        detail.append(f"{step.duration_h} h")
    return f"{step.text} ({', '.join(detail)})"


def _attribute_block(reaction: OrdReaction) -> str:
    """Render whatever the source recorded that this schema has no field for.

    Last in the body so unmodelled fields never push the recipe out of the character-prefix
    retrieval excerpt. A definition list keeps the source's own labels rather than inventing names.
    """
    if not reaction.attributes:
        return ""
    lines = "".join(f"- {key}: {value}\n" for key, value in reaction.attributes.items())
    return f"\n## Recorded fields\n\n{lines}"
