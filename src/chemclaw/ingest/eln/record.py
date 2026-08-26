"""Map a canonical ORD reaction to an ELN transcription record (D-2026-08-25).

The pure mapping from an `OrdReaction` to the `ReactionRecord` the transcription tier stores. It
records the reaction SMILES, headline conditions (scale first), the charge sheet behind that scale,
the impurity profile, and the full **step-by-step procedure** in prose, so a detailed development
recipe survives ingestion intact and a chemist who reaches this record from a structure search gets
the recipe rather than an id.

**Nothing here infers anything**, which is why the result is data rather than a PR-gated note
(`chemclaw.ingest.eln.records`): every field is read from the entry or rendered from fields that
were, so there is no claim for a reviewer to accept or reject. What a human *asserts* about these
runs is a playbook or a campaign in `knowledge/`, gated as it always was, citing this record as
`reaction-<id>`.

The record carries no `[[wikilink]]`, and that is enforced by `_without_wikilinks` rather than
merely asserted — the source's free text reaches this body verbatim, and a record that could spell
a relation would let an ELN write an edge into the graph that cites it.
"""

import re
from typing import Literal, assert_never

from chemclaw.ingest.eln.ord import (
    Component,
    Impurity,
    OrdReaction,
    OutcomeClass,
    ReactionStep,
    Role,
)
from chemclaw.ingest.eln.records import ReactionRecord
from chemclaw.kg.note import ProcessConditions

# Each `[` that another `[` follows. A lookahead so the match consumes one character and the next
# is re-examined, which is what makes the substitution unable to manufacture the delimiter it is
# removing — see `_without_wikilinks`.
_OPENING_BRACKET_PAIR = re.compile(r"\[(?=\[)")


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

    **A lookahead, not `str.replace("[[", "[ [")`, and that distinction is the whole control.**
    `str.replace` scans left to right and never re-reads what it has just emitted, so it consumes
    the *first* two brackets of `[[[` and appends the third untouched: `[[[x]]` becomes `[ [[x]]`,
    which contains a brand-new valid delimiter and forges the edge anyway. Substituting each `[`
    that is *followed* by a `[` cannot be outrun that way, because the decision is made per
    character against the original text. Measured over 200,000 random bracket-dense bodies: the
    replace form leaks 5, this form leaks 0.
    """
    return _OPENING_BRACKET_PAIR.sub("[ ", body)


def record_from_ord_reaction(reaction: OrdReaction) -> ReactionRecord:
    """Map an `OrdReaction` to the transcription record the corpus stores (idempotent id)."""
    body = _without_wikilinks(
        f"Reaction `{reaction.reaction_smiles()}` from ELN entry {reaction.reaction_id}.\n\n"
        f"{_hypothesis_block(reaction)}"
        f"{_conditions_block(reaction)}"
        f"{_charge_block(reaction)}"
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
        # The project is the one grouping key the entry already carries, and the whole of what a
        # `tag=` filter narrows on here. Exactly the project and nothing else — a derived
        # vocabulary (a scale band, an outcome word) would be a taxonomy this mapping invented,
        # filterable against a scheme no chemist agreed to and no other note class uses.
        project=reaction.project or None,
        # The experiment's own date is what makes the record time-scopable (gap KNW-1): "what have
        # I tried on this step in the last two weeks" is a `since`/`until` window over this column.
        # A run is evidence from the day it was run, and it has no expiry — a result does not
        # lapse on its own, it is superseded, which is a claim a human makes in a note.
        performed_at=reaction.performed_at,
        # The numbers a chemist compares, kept as numbers. The body renders them as prose for a
        # human; this is the same facts in the form anything comparing runs can read without
        # re-deriving them from the sentences it just wrote (`ProcessConditions` says why).
        #
        # `ProcessConditions` argues for frontmatter over "a second table", and that argument
        # survives the move rather than being overridden by it: the reaction row already exists
        # (D-2026-08-25), so these ride on it and there is still exactly one place a run's numbers
        # live. What it rejected was a store *just* for conditions, which this is not.
        conditions=_conditions(reaction),
        body=body,
    )


def _stated_outcome(
    outcome: OutcomeClass | None,
) -> Literal["success", "failure", "inconclusive"] | None:
    """The frontmatter spelling of an outcome a source stated, or `None` when it stated none.

    **A `match` rather than a dict lookup, so the exhaustiveness claim is one mypy actually makes.**
    The frontmatter type is a `Literal` and this mapping is where a new `OutcomeClass` member has to
    be given a spelling. Written as a `dict[OutcomeClass, Literal[...]]` that intent was a comment
    and nothing more: mypy does not exhaustiveness-check a dict literal's keys, so a fourth member
    would have type-checked clean and raised `KeyError` here at runtime — inside
    `record_from_ord_reaction`, outside the `ElnMappingError` path that rejects one entry and
    continues, so it would have aborted the whole sync rather than one row. `assert_never` moves
    that back to where the comment always claimed it was.

    **`SUCCESS` is spelled here now**, where it used to be omitted so a record would not assert a
    success the ELN never claimed. That was necessary while the field defaulted to SUCCESS, and it
    cost the distinction between a run the chemist recorded as successful and one nobody assessed.
    With `outcome_class` optional (`D-2026-08-26-silence-is-not-a-successful-run`) silence is
    carried by `None`, and a stated success can be written as what it is.
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

    `None` rather than an all-empty block: a note carrying `conditions: {}` would claim the
    question was asked and answered emptily, where the honest reading is that this note is not
    about a run with recorded conditions at all. Same rule as `_quality_columns` dropping a column
    nothing filled.

    `outcome` is written when the source stated one and left `None` when it did not — the same
    "absent means nobody wrote it down" rule as every other field here, now that `outcome_class`
    can say that (`D-2026-08-26-silence-is-not-a-successful-run`).
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
    # An explicit field check rather than a `model_dump(exclude_none=True)`: serializing the whole
    # model to ask whether any of it is set is work for an answer the fields already give.
    return conditions if any(dict(conditions).values()) else None


def _principal_product(reaction: OrdReaction) -> str | None:
    """The molecule this record is *about*, when the entry names exactly one product.

    The column every by-compound question starts from: "what else have we made this way", and the
    join a playbook uses when it groups runs by what they produced. The structure is in the entry
    either way — it goes into the body as part of the reaction SMILES — this is what puts it
    somewhere a query can reach without parsing prose.

    **Only when there is one outcome.** "The molecule this record is about" has no honest answer
    for a reaction reporting a product and two by-products, and picking the first (or the largest
    by amount, which an ELN often omits) would file the run under a compound the chemist did not
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
    if reaction.outcome_class in (OutcomeClass.FAILURE, OutcomeClass.INCONCLUSIVE):
        # Stated first-class in the body, not implied by a missing yield: a reader (and retrieval)
        # must be able to tell "this did not work" from "nobody recorded the number" (gap KNW-3).
        # A stated success is deliberately not written here — the body lists what a chemist would
        # read as notable, and the frontmatter carries the value for anything comparing runs. An
        # *unstated* outcome writes nothing at all, exactly like an unrecorded temperature.
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
    """Render the recipe: the ordered steps, the prose the source recorded, or both.

    **The prose branch exists because without it a whole class of source lost its protocol
    silently.** This function used to return `""` whenever `steps` was empty, and
    `chemclaw.ingest.eln.warehouse.binding` excludes `steps` from what a binding may map, on the
    stated grounds that "a warehouse records a protocol as prose, which lands in
    `procedure_text` verbatim". Both
    statements were true and nothing rendered that prose: measured, a warehouse-shaped reaction
    carrying 251 characters of procedure produced a 63-character note body containing none of it,
    and `procedure_text` had three writers and exactly one reader in the tree — a 240-character
    excerpt in `memory/optimization`. For the warehouse ELN — the first live connector —
    `expand_note` answered with a reaction that had no recipe.

    **Why both are sometimes rendered, decided by containment rather than by a threshold.** The two
    file-drop adapters populate `steps` *and* `procedure_text`, and they do it differently.
    `json_adapter` segments the prose, so its steps are that prose recut — measured, 0.992 string
    similarity, and every step's text appears verbatim inside it. `ord_adapter` derives steps from
    structured ORD fields while `procedure_text` is the chemist's own `notes.procedure_details` —
    measured, 0.555 similarity, with the steps reading `Add CCO` where the prose reads "a catalytic
    amount of sulfuric acid over 30 min". Rendering steps alone would have dropped that sentence,
    which is the same defect one source over; rendering both always would duplicate the whole
    recipe on every `json_adapter` note.

    So the question asked is the exact one that matters — *are these steps a cut of this prose?* —
    and it is answered by containment, which needs no tuned number and cannot drift: if every step's
    text is inside the prose, the steps are the better presentation of it and stand alone.
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

    True when every step's text appears verbatim in the whitespace-normalized prose — which is what
    a segmenter produces and what a mapper reading structured fields cannot. `_procedure_block`
    says why this is the question and why containment is how it is asked.
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
