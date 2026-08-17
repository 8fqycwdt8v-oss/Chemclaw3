"""Map a canonical ORD reaction to an agent knowledge-graph note (plan step 4.5).

The pure mapping from an `OrdReaction` to a `reaction` note, proposed through the **same**
PR-gate as every other agent note (D-005) — no second write path. Kept separate from the
sync activity so it is tested directly. The note records the reaction SMILES, headline
conditions (scale first), the charge sheet behind that scale, and the full **step-by-step
procedure** in prose so a detailed development recipe survives ingestion intact (a human
reviewer signs off on the recipe, not just a SMILES). Its `tags` are the record's project, so
the documented project filter (`gather_evidence(tag=…)`) reaches reaction notes at all. It
carries no `[[wikilink]]` (a dangling link would fail `chemclaw.kg.validate` on the very PR this
opens); compound cross-links are a later step once compound notes exist. That last sentence is
enforced by `_without_wikilinks` rather than merely asserted — the source's free text reaches this
body verbatim, so until it was enforced the ELN could spell a relation the graph then believed.
"""

from chemclaw.ingest.eln.ord import (
    Component,
    Impurity,
    OrdReaction,
    OutcomeClass,
    ReactionStep,
    Role,
)
from chemclaw.kg.note import Note, note_id_for_reaction


def _without_wikilinks(body: str) -> str:
    """Neutralize any `[[rel:id]]` span the source's own free text spelled.

    This module's docstring promises the note "carries no `[[wikilink]]`", and that promise was
    false: `kg.note` parses the rendered body for links, so a chemist typing
    `[[contradicts:reaction-1234]]` into a hypothesis, a failure reason, a procedure step or an
    unmapped attribute forged a real relation into a PR-gated note. The gate cannot catch it — a
    forged link is indistinguishable from an authored one, `contradicts` and `supersedes` are in
    the allowed vocabulary, and `kg.validate` only objects when the target does not exist, so
    naming a *real* note passes review as a well-formed note. Which it is.

    Applied once to the assembled body rather than at each of the five free-text sites, so the next
    field added to this mapping cannot forget it — and so that the values which are not obviously
    free text (`reaction_id`, an attribute *key*) are covered by the same line as the ones that are.
    A cross-block spelling was considered and is not the reason: the blocks are joined by newlines
    and label prefixes, so two `[` from adjacent fields never actually meet.

    The substitution is visible and lossless rather than a strip. The note is prose a human signs
    off on, so the reviewer should see what the source actually wrote — and deleting a chemist's
    characters to make them safe is the same mistake as trusting them.
    """
    return body.replace("[[", "[ [")


def note_from_ord_reaction(reaction: OrdReaction) -> Note:
    """Map an `OrdReaction` to an agent-authored `reaction` note (idempotent id)."""
    body = _without_wikilinks(
        f"Reaction `{reaction.reaction_smiles()}` from ELN entry {reaction.reaction_id}.\n\n"
        f"{_hypothesis_block(reaction)}"
        f"{_conditions_block(reaction)}"
        f"{_charge_block(reaction)}"
        f"{_impurity_block(reaction)}"
        f"{_procedure_block(reaction)}"
        f"{_attribute_block(reaction)}"
    )
    return Note(
        id=note_id_for_reaction(reaction.reaction_id),
        type="reaction",
        created_by="agent",
        source=reaction.provenance,
        compound_smiles=_principal_product(reaction),
        # The project is the one grouping key the record already carries, and it reached the graph
        # from nowhere on the largest note class: `gather_evidence(tag=…)` is documented as the
        # project filter and matched nothing on reactions. Exactly the project and nothing else —
        # a derived tag vocabulary (a scale band, an outcome word) would be a taxonomy this mapping
        # invented, filterable against a scheme no chemist agreed to and no other note class uses.
        # `memory.campaign` already tags its notes with the projects behind them; one convention.
        tags=[reaction.project] if reaction.project else [],
        # The experiment's own date is what makes the note time-scopable (gap KNW-1). F10-G2 added
        # `valid_from`/`valid_to` to answer "what did we know at time T", and for the largest note
        # class nothing populated them — a reaction became valid-since-forever. A run is evidence
        # from the day it was run; `valid_to` stays open (a result does not expire on its own, it
        # is superseded, which is a separate edit).
        valid_from=reaction.performed_at,
        body=body,
    )


def _principal_product(reaction: OrdReaction) -> str | None:
    """The molecule this note is *about*, when the record names exactly one product.

    The largest note class in the graph carried no `compound_smiles` at all, which is why nothing
    that groups by compound could ever see a reaction: `kg.conflicts` groups on
    `(type, compound_smiles)`, `find_notes` searches it, and every future by-compound question
    starts there. The structure was in the record the whole time — it goes into the body as part
    of the reaction SMILES — it simply never reached the field.

    **Only when there is one outcome.** "The molecule this note is about" has no honest answer for
    a reaction that reports a product and two by-products, and picking the first (or the largest
    by amount, which an ELN often omits) would file the note under a compound the chemist did not
    mean. A wrong `compound_smiles` is worse than none: it is what a by-compound search would
    return, and it would look right.
    """
    if len(reaction.outcomes) != 1:
        return None
    return reaction.outcomes[0].smiles


def _hypothesis_block(reaction: OrdReaction) -> str:
    """Lead with what the run was testing, when the source recorded it (D-162).

    First in the body rather than buried among the conditions, because it is what makes the run
    legible: every condition below is an answer, and this is the question. Empty when unrecorded —
    the note never says "no hypothesis", which would assert something the record does not.
    """
    if not reaction.hypothesis:
        return ""
    return f"Tested: {' '.join(reaction.hypothesis.split())}\n\n"


def _conditions_block(reaction: OrdReaction) -> str:
    """Render the headline conditions (scale/temperature/time/yield) as a bullet list."""
    conditions = []
    if (scale := _scale(reaction)) is not None:
        conditions.append(f"scale: {scale}")
    if reaction.temperature_c is not None:
        conditions.append(f"temperature: {reaction.temperature_c} °C")
    if reaction.time_h is not None:
        conditions.append(f"time: {reaction.time_h} h")
    if reaction.yield_percent is not None:
        conditions.append(f"yield: {reaction.yield_percent}%")
    if reaction.purity_percent is not None:
        conditions.append(f"purity: {reaction.purity_percent}%")
    if reaction.performed_at is not None:
        conditions.append(f"performed: {reaction.performed_at.isoformat()}")
    if reaction.outcome_class is not OutcomeClass.SUCCESS:
        # Stated first-class in the body, not implied by a missing yield: a reader (and retrieval)
        # must be able to tell "this did not work" from "nobody recorded the number" (gap KNW-3).
        outcome = f"outcome: {reaction.outcome_class.value}"
        if reaction.failure_reason:
            outcome += f" — {reaction.failure_reason}"
        conditions.append(outcome)
    return "".join(f"- {c}\n" for c in conditions)


def _scale(reaction: OrdReaction) -> str | None:
    """The run's scale, as one bullet at the *top* of the conditions.

    Charged amounts were on the record and reached the note only if the chemist happened to write
    them into the procedure prose, so nothing — not retrieval, not the agent, not a reader skimming
    a hit — could tell a 5 g proof-of-concept from a 2 kg pilot batch without reading the whole
    body. Scale is the context every other condition is read against: 100 °C for 4 h means one
    thing on a bench run and another in a reactor.

    **Reactants only**, because that is what a chemist means by "a 5 g run". Solvent mass is the
    bulk of any flask and tracks the vessel, not the amount of material being made; folding it in
    would report the same number for a dilute 5 g run and a concentrated 50 g one. Reagents are
    excluded for the same reason in reverse — three equivalents of an inorganic base can outweigh
    the substrate and would inflate the figure well past what anyone would call the scale.

    Mass preferred, `amount_mmol` as the fallback, and both unit-labelled so the two forms are
    never confused. `None` when the record charges neither — the note stays silent rather than
    asserting a scale it does not know.

    **The two forms are chosen per record, not per reactant, and a record carrying both reports
    both.** `Component` allows `mass_mg` and `amount_mmol` independently, so "an ELN records one or
    the other" is true of most records and not of the schema. Preferring mass whenever *any*
    reactant had one silently dropped every reactant that had only moles: a run charging 4.6 g of
    one substrate and 120 mmol (≈7.2 g) of another reported "4.6 g", a 2.5x under-report of the one
    number this bullet exists to make legible. Under-reporting scale is the specific direction that
    matters — it makes a pilot batch read as a bench run.

    **First in the block, and in the block rather than in a tag.** A retrieval excerpt is a blind
    character prefix of the body (`retrieval.retrievers._excerpt`, `note_excerpt_chars`), so
    anything appended at the end is invisible to exactly the notes with the most prose — the
    detailed ones. A tag would be worse still: tags are matched by equality, so a usable scale
    tag means inventing bands ("bench", "kilo") that no chemist agreed to and no other note class
    uses; the per-input detail below is what a machine reads, this line is what a skim reads.
    """
    reactants = [c for c in reaction.inputs if c.role is Role.REACTANT]
    masses = [c.mass_mg for c in reactants if c.mass_mg is not None]
    # Only those with no mass, so a reactant carrying both is counted once, on the preferred form.
    amounts = [c.amount_mmol for c in reactants if c.mass_mg is None and c.amount_mmol is not None]
    grams = f"{sum(masses) / 1000:g} g" if masses else ""
    mmol = f"{sum(amounts):g} mmol" if amounts else ""
    charged = " + ".join(part for part in (grams, mmol) if part)
    if not charged:
        return None
    return f"{charged} of reactants charged"


def _charge_block(reaction: OrdReaction) -> str:
    """Render what was actually charged, per input — the machine-legible form of the scale.

    The `scale:` bullet is one number for a skim; this is the charge sheet behind it, so a reader
    (or a downstream consumer) can see which species carried the mass and recompute stoichiometry
    instead of taking a derived figure on trust. Empty when no input carries an amount, so a
    record that never reported one gets no section rather than a table of blanks.

    Every input is listed once the section exists, including those with no recorded amount: that a
    species was charged is itself information, and omitting its row would read as "not charged".
    """
    if not any(c.mass_mg is not None or c.amount_mmol is not None for c in reaction.inputs):
        return ""
    lines = "".join(f"- {_charge_line(c)}\n" for c in reaction.inputs)
    return f"\n## Charge\n\n{lines}"


def _charge_line(component: Component) -> str:
    """One charged species: its structure, its role, and whatever amount was recorded."""
    amounts = []
    if component.mass_mg is not None:
        amounts.append(f"{component.mass_mg:g} mg")
    if component.amount_mmol is not None:
        amounts.append(f"{component.amount_mmol:g} mmol")
    detail = ", ".join(amounts) if amounts else "amount not recorded"
    line = f"`{component.smiles}` ({component.role.value}): {detail}"
    # Whatever else the source recorded about this species, on the row it belongs to rather than in
    # the reaction-level block: a lot number is a fact about *this* charge, and hoisting it would
    # lose which species it described the moment a record charges two lots of the same reagent.
    if component.attributes:
        line += " — " + _attribute_text(component.attributes)
    return line


def _attribute_text(attributes: dict[str, str]) -> str:
    """Render an attribute bag as `key: value` pairs, in the order the binding produced them."""
    return ", ".join(f"{key}: {value}" for key, value in attributes.items())


def _impurity_block(reaction: OrdReaction) -> str:
    """Render the impurity profile, the half of the outcome yield alone never captures.

    Rendered into the note body (not only frontmatter) because retrieval reads bodies: an
    impurity-driven question — "what did we see besides product on that route?" — has to be able
    to match here.
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
    """Render the ordered procedure as a numbered list (empty when there are no steps)."""
    if not reaction.steps:
        return ""
    lines = "".join(f"{step.index}. {_step_line(step)}\n" for step in reaction.steps)
    return f"\n## Procedure\n\n{lines}"


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

    **Last in the body, deliberately.** A retrieval excerpt is a blind character prefix
    (`retrieval.retrievers._excerpt`, `note_excerpt_chars`), so every block competes for the same
    budget. These are the fields nobody has yet decided are worth a question — putting them ahead of
    the procedure would push the actual recipe out of the excerpt to make room for a vessel id.

    Rendered as a definition list rather than prose so the labels stay the source's own. This
    section is the record saying "the ELN also carried these"; inventing readable names for them
    here would assert a mapping nobody wrote.
    """
    if not reaction.attributes:
        return ""
    lines = "".join(f"- {key}: {value}\n" for key, value in reaction.attributes.items())
    return f"\n## Recorded fields\n\n{lines}"
