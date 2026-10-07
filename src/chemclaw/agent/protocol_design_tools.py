"""The agent's way to *write* a protocol, having spent the turn reading the record.

The writing half beside `agent/protocol_tools.py`: the structured ask, the design that comes out
of it, and the revisions an edit produces. Nothing here decides chemistry; that judgment lives in
`skills/protocol-generation` and `skills/hte-campaign-design`. What is here is the shape of the
answer, the checks it must survive, and the store it lands in. `checks.evidence_present` is a
blocker, so a design citing no precedent and no tool cannot be stored.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, computed_field

from chemclaw.agent.authz import require_actor
from chemclaw.agent.framing import defang
from chemclaw.agent.session_store import owner_permits
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import get_current_correlation_id
from chemclaw.core.metrics_bridge import degraded
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.tool_registry import tool
from chemclaw.core.turn_text import get_current_user_texts
from chemclaw.kg.graph import load_notes
from chemclaw.kg.note import external_record_ref
from chemclaw.memory.failure import failures_against, observation_of
from chemclaw.protocols.checks import (
    blockers,
    run_checks,
    used_structures,
)
from chemclaw.protocols.diff import diff_designs
from chemclaw.protocols.export import run_sheet_path
from chemclaw.protocols.from_bo import factors_and_arms
from chemclaw.protocols.layout import LayoutError, place, smallest_plate_for
from chemclaw.protocols.models import (
    DesignRevision,
    DesignStatus,
    DesignSummary,
    EvidenceRef,
    ExperimentDesign,
    ExperimentRequest,
    Factor,
    PlateLayout,
    ProtocolArm,
    ProtocolBody,
    RecordedFailure,
    UncitedPrecedent,
    design_id_for,
)
from chemclaw.protocols.render import (
    ProtocolReadout,
    receipt,
    render_markdown,
)
from chemclaw.protocols.rescale import RescaleError, rescale
from chemclaw.protocols.result_store import default_arm_result_store
from chemclaw.protocols.results import (
    ArmResult,
    MixedUnits,
    PlateOutcomes,
    UnknownArm,
    observations_for,
    require_arms_exist,
    summarise,
)
from chemclaw.protocols.store import DesignStore, RevisionConflict, default_design_store
from chemclaw.science.bo.campaign_record import read_campaign_thread
from chemclaw.science.fingerprints.rxnfp.search import find_similar_reactions
from chemclaw.science.fingerprints.store import default_reaction_store

logger = logging.getLogger(__name__)

#: The most designs one listing returns. A chemist scanning a list wants the recent ones; anything
#: longer is a query with a filter on it.
_LISTING_LIMIT = 50


def _readable(document: BaseModel) -> str:
    """`document` as the JSON a tool returns, with any envelope delimiter in it neutralised.

    Every model here carries model-written free text that is stored and replayed into later turns.
    Defanged rather than framed, since a design is this system's own document, not evidence; the
    whole
    payload rather than a field list, so new string fields are covered.
    """
    return defang(document.model_dump_json())


def _store() -> DesignStore:
    return default_design_store()


# A figure somebody wrote as a quantity, for relating a stated value to the words quoted for it.
# Digits welded to letters (SMILES ring closures, `C18`) and the halves of a decimal are not
# figures; digits beside punctuation (`96-well`, `2 g`, ISO dates) are.
_DIGITS = re.compile(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?(?![A-Za-z])")


# Figures written as words, so "five grams" still states a scale the model normalised to `5 g`.
_NUMBER_WORDS = frozenset(
    """zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen
    fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty
    ninety hundred thousand dozen half quarter single double triple""".split()
)

#: Alphanumeric runs, over text already lowercased — the tokens a value and a quote are compared as.
_TOKEN = re.compile(r"[a-z0-9]+")


def _quote_supports(value: str, quote: str) -> bool:
    """Whether these words plausibly state this value.

    A quote that merely occurs in the chemist's message proves nothing about the value, so the two
    are related for every quote:

    1. **A value carrying figures needs the quote's figures to be its own**, compared as numbers
       (`'09'` meets `'9'`).
    2. **A figure written in words satisfies rule 1** ("five grams" for `'5 g'`).
    3. **A value with no figures needs the quote to carry its words**: the same token, or one
       containing the other when long enough to mean something.

    A heuristic: it refuses quotes that cannot state the value, but cannot tell whether a figure the
    quote does carry is about this slot.
    """
    value_numbers = {float(digits) for digits in _DIGITS.findall(value)}
    quote_tokens = set(_TOKEN.findall(quote.lower()))
    if value_numbers:
        quote_numbers = {float(digits) for digits in _DIGITS.findall(quote)}
        if quote_numbers:
            return bool(value_numbers & quote_numbers)
        return bool(quote_tokens & _NUMBER_WORDS)
    return any(
        value_token == quote_token
        or (len(value_token) >= 4 and (value_token in quote_token or quote_token in value_token))
        for value_token in _TOKEN.findall(value.lower())
        for quote_token in quote_tokens
    )


def _said_somewhere(quote: str, haystacks: Sequence[str]) -> bool:
    """Whether the chemist wrote these words, in this order, in any *one* of their messages.

    Per message, so a quote spanning two messages is refused. The caller normalises whitespace.
    """
    needle = " ".join(quote.split()).lower()
    return any(needle in haystack for haystack in haystacks)


def require_quotes_are_verbatim(
    request: ExperimentRequest, source_texts: tuple[str, ...] | None
) -> None:
    """Refuse a `basis="stated"` slot whose quote is not in the chemist's own words.

    A `stated` slot claims "the chemist wrote this", so the quote must be verbatim (whitespace
    normalised, no paraphrase) and must support the value (`_quote_supports`). `source_texts` is the
    thread's user turns from `core.turn_text`, ambient rather than a tool argument the model could
    fill; each message is a separate haystack. `None` (no conversation) refuses every `stated` slot,
    following `require_actor`'s reject-if-absent rule.

    Raises:
        ChemclawError: naming the slot and its quote.
    """
    stated = {
        name: field
        for name, field in (
            ("scale", request.scale),
            ("plate_format", request.plate_format),
            ("max_runs", request.max_runs),
            ("deadline", request.deadline),
        )
        if field.basis == "stated"
    }
    if not source_texts:
        if stated:
            raise ChemclawError(
                "these slots are marked `stated` but there is no chemist message to check them "
                "against: "
                + ", ".join(sorted(stated))
                + ". `stated` means the chemist wrote it; use basis='inferred' for your own "
                "judgment."
            )
        return
    haystacks = [" ".join(said.split()).lower() for said in source_texts]
    slots = {
        "scale": request.scale,
        "plate_format": request.plate_format,
        "max_runs": request.max_runs,
        "deadline": request.deadline,
    }
    missing = [
        f"{name}: {field.quote!r}"
        for name, field in slots.items()
        if field.basis == "stated" and not _said_somewhere(field.quote, haystacks)
    ]
    if missing:
        raise ChemclawError(
            "these slots are marked `stated` but their quote is not in anything the chemist has "
            f"written in this conversation ({len(haystacks)} of their messages checked, this "
            "turn's included): "
            + "; ".join(missing)
            + ". Only their own words are checkable — not your earlier prose, not a tool result, "
            "and not a message older than the window this conversation keeps — so ask them to "
            "restate it if it matters. Use basis='inferred' for your own judgment and quote the "
            "chemist verbatim when you mark something stated."
        )
    unsupported = [
        f"{name}: value {field.value!r} is not supported by quote {field.quote!r}"
        for name, field in slots.items()
        if field.basis == "stated" and not _quote_supports(field.value, field.quote)
    ]
    if unsupported:
        raise ChemclawError(
            "these slots are marked `stated` but their quote does not say their value: "
            + "; ".join(unsupported)
            + ". The quote has to be the words that state the value, not any words from the "
            "message. Use basis='inferred' when the value is your own reading."
        )


async def _require_writable(store: DesignStore, design_id: str) -> DesignSummary | None:
    """The design's header, refusing the write when this actor does not own the design.

    Applies `owner_permits`, the same rule as the HTTP layer, to an explicit `design_id`.
    `design_id_for` scopes derived ids by owner; this holds when an id is passed in. A design with
    no
    header yet (`None`) is writable: that write creates it and records the owner.
    """
    header = await store.summary(design_id)
    if header is not None and not owner_permits(header.opened_by, require_actor()):
        raise ChemclawError(
            f"{design_id} belongs to another chemist. Open your own design for this ask with "
            "`structure_experiment_request` rather than writing to theirs."
        )
    return header


async def _read_design_or_refuse(
    store: DesignStore, design_id: str, revision: int = 0
) -> DesignRevision:
    """One revision of a design — the head for `revision=0` — or a refusal the model can act on.

    Shared by the four reading tools. The message names the requested revision, if any, and the tool
    that lists what exists.
    """
    stored = await store.read(design_id, revision or None)
    if stored is None:
        raise ChemclawError(
            f"no design {design_id!r}"
            + (f" at revision {revision}" if revision else "")
            + ". Use find_experiment_protocols to list what exists."
        )
    return stored


async def _stored_status(store: DesignStore, design_id: str) -> DesignStatus:
    """The design's status as the store holds it — never a default this function invented.

    Callers reach this after a revision exists, so a missing header is an inconsistency, not a
    `requested` design.

    Raises:
        ChemclawError: the design has revisions and no header row.
    """
    header = await store.summary(design_id)
    if header is None:
        raise ChemclawError(
            f"{design_id} has revisions but no header row, so its status cannot be read; "
            "the store is inconsistent"
        )
    return header.status


async def recorded_failures(design: ExperimentDesign) -> list[RecordedFailure]:
    """What the corpus already records as having failed, for the citations and reagents in `design`.

    The seam between the pure check `no_documented_failure` (in `protocols`, which may import only
    `core` and `science`) and the knowledge graph (`memory/failure.failures_against`). Offloaded to
    a
    thread because loading notes parses the corpus off disk. Never raises: an unreadable corpus says
    less rather than refusing a design, and is counted through `degraded()`.
    """
    cited = [ref.ref for ref in design.evidence if ref.ref]
    structures = [smiles for _, smiles in used_structures(design)]
    if not cited and not structures:
        return []
    try:
        notes = await asyncio.to_thread(
            lambda: failures_against(
                load_notes(settings.knowledge_path), cited=cited, structures=structures
            )
        )
    except Exception as exc:
        degraded(
            logger,
            "failure_memory",
            "could not read the corpus for recorded failures, so this design was checked "
            "without them: %s",
            exc,
        )
        return []
    # Inside the guard because the reduction can raise too (`RecordedFailure.id` requires a
    # non-empty
    # id), and callers rely on this never raising.
    try:
        return [RecordedFailure(id=note.id, summary=observation_of(note)) for note in notes]
    except ValidationError as exc:
        degraded(
            logger,
            "failure_memory",
            "the corpus returned a failure record this check cannot read, so this design was "
            "checked without it: %s",
            exc,
        )
        return []


async def uncited_precedent(design: ExperimentDesign) -> list[UncitedPrecedent]:
    """Runs the record already holds that resemble this design and that it does not cite.

    The same seam shape as `recorded_failures`, for `precedent_consulted`. It offers and never
    cites:
    writing hits into `design.evidence` would let `evidence_present` pass on an ungrounded design.
    Already-cited hits are dropped by comparing in the citation's own spelling
    (`external_record_ref`). Never raises; failures are counted through `degraded()`.
    """
    reaction = design.request.reaction_smiles.strip()
    if not reaction:
        return []
    cited = {external_record_ref(ref.ref)[1] for ref in design.evidence if ref.ref}
    try:
        search = await find_similar_reactions(default_reaction_store(), reaction)
    except Exception as exc:
        degraded(
            logger,
            "precedent_lookup",
            "could not search the reaction index for precedent, so this design was checked "
            "without it: %s",
            exc,
        )
        return []
    # Inside the guard: `Match.similarity` is an unconstrained float while
    # `UncitedPrecedent.similarity`
    # is bounded to [0, 1], so a rounding overshoot or `nan` would raise.
    try:
        return [
            UncitedPrecedent(id=hit.id, similarity=hit.similarity, label=hit.label)
            for hit in search.hits
            if hit.id not in cited
        ]
    except ValidationError as exc:
        degraded(
            logger,
            "precedent_lookup",
            "the reaction index returned a hit this check cannot read, so this design was "
            "checked without it: %s",
            exc,
        )
        return []


@tool
async def structure_experiment_request(request: ExperimentRequest, salt: str = "") -> str:
    """Turn a chemist's free-text ask into the structured request a protocol is drafted from.

    Call this **first**, before searching the record: it puts your reading of their sentence in
    front of them while correcting it is still cheap, and it returns the `design_id` every later
    call needs.

    Mark each slot's `basis` honestly — `stated` obliges their verbatim words in `quote`, checked
    against what the chemist has written in this conversation (earlier turns included) and refused
    if it is not there; `inferred` is your own judgment and is expected; `absent` means the text
    did not say. Resolve species with
    `resolve_compound`; never write a SMILES from a name. `skills/protocol-generation` has the rest.

    Args:
        request: The structured ask.
        salt: Only to open a *second* design for the same ask. The id is derived from the title,
            goal, transformation and mode, so correcting any of those opens a new design rather
            than revising this one — say so to the chemist when it happens.

    Returns:
        JSON: the design id, the revision and the checks. Show the chemist your structured reading
        and let them correct it before you draft.

    Raises:
        ChemclawError: a `stated` slot whose quote is not in the chemist's own message, or a
            design of that ask belonging to another chemist.
    """
    require_quotes_are_verbatim(request, get_current_user_texts())
    # Scoped by the actor, so two chemists phrasing one ask the same way get two designs rather
    # than one they overwrite in turn.
    design_id = design_id_for(request, owner=require_actor(), salt=salt)
    store = _store()
    await _require_writable(store, design_id)
    head = await store.read(design_id)
    # Re-structuring the same ask reaches the same design id, so the corrected ask lands and the
    # existing procedure and plate are carried forward. Checks are graded at the design's stage, so
    # a
    # protocol contradicting the corrected ask is visible.
    design = (
        head.design.model_copy(update={"request": request})
        if head is not None
        else ExperimentDesign(request=request)
    )
    # An identical document is not a revision; appending one would retire an `approved` status for
    # no
    # change.
    if head is not None and design == head.design:
        return _readable(
            receipt(
                design,
                head.checks,
                design_id=design_id,
                revision=head.revision,
                status=await _stored_status(store, design_id),
            )
        )

    checks = run_checks(
        design,
        stage="protocol" if design.has_protocol else "request",
        failures=await recorded_failures(design),
        precedent=await uncited_precedent(design),
    )
    revision = await store.append(
        design_id,
        design,
        checks,
        author_kind="agent",
        author=require_actor(),
        parent_revision=head.revision if head else 0,
        change_note="structured the request" if head is None else "restructured the ask",
        session_id=get_current_session_id() or "",
        correlation_id=get_current_correlation_id() or "",
        status="requested",
    )
    return _readable(
        receipt(
            design,
            checks,
            design_id=design_id,
            revision=revision.revision,
            status=await _stored_status(store, design_id),
        )
    )


@tool
async def draft_experiment_protocol(
    design_id: str,
    parent_revision: int,
    base: ProtocolBody,
    evidence: list[EvidenceRef],
    change_note: str,
    factors: list[Factor] | None = None,
    arms: list[ProtocolArm] | None = None,
    plate_format: int = 0,
    randomize_run_order: bool = False,
    seed: int | None = None,
) -> str:
    """Store a protocol — a single experiment or a whole screening plate — and check it.

    One tool for both, because they are one object: a single experiment has **no factors and no
    arms**; a screen is the same protocol with factors, levels, N arms and a plate. It creates and
    revises alike — `parent_revision` is what you are building on, and one that is not the head is
    refused rather than allowed to overwrite somebody else's edit.

    `structure_experiment_request` comes first and supplies `design_id`; this tool takes no ask,
    because the design already holds the one the chemist corrected.

    **Do the work before you call this: a design citing no precedent and no tool is refused.**
    Search the record (`substrate_precedent`, `conditions_for_similar_reaction`,
    `reagent_frequency`, `workup_precedent`, `condense_protocols`), compute what it does not state,
    screen for hazards, then cite what you used. `skills/protocol-generation` and
    `skills/hte-campaign-design` hold the judgment.

    Args:
        design_id: The `design-…` id `structure_experiment_request` returned.
        parent_revision: The revision you are building on. The error names the head if it moved.
        base: The protocol every arm shares — setpoints, charge table, steps, analytics, hazards.
        evidence: The precedent and tool citations behind these conditions. At least one of each,
            each naming in `supports` the part of the design it is offered for.
        change_note: What this revision does and why.
        factors: What a screen varies. Omit for a single experiment.
        arms: One per set of conditions, each setting every factor. Omit for a single experiment.
        plate_format: 24, 48, 96, 384 or 1536 to lay the arms out. The error names the smallest
            plate that fits when they do not. **Omitting it on a revision carries the previous
            plate forward unchanged**, which is what you want when only a temperature moved — and
            is wrong the moment the set of arms changes, because the carried-forward layout then
            leaves a new arm with no well or names a well for an arm that is gone, and the draft is
            refused with a message about wells. Pass a `plate_format` whenever you add or remove an
            arm, and the plate is laid out again.
        randomize_run_order: Shuffle the order the arms are *run* in, never their well positions —
            what stops a drift over the session from reading as a factor effect.
        seed: Required when randomizing, so the plate a chemist ran can be reproduced.

    Returns:
        JSON: the design id, the revision, every check with its verdict, and the first arms as a run
        sheet. Read the checks back — a warning is the chemist's judgment, not one to suppress.

    Raises:
        ChemclawError: no such design, the design belongs to another chemist, a blocking check
            failed, the plate cannot hold the arms, or the revision is derived from something that
            is no longer the head.
    """
    if not change_note.strip():
        raise ChemclawError(
            "every revision needs a change_note saying what it does and why — including the "
            "first draft, which is revision one of the protocol rather than a special case"
        )
    store = _store()
    await _require_writable(store, design_id)
    previous = await store.read(design_id)
    if previous is None:
        raise ChemclawError(
            f"no design {design_id!r}. Call structure_experiment_request first — it structures the "
            "chemist's ask and returns the id this tool drafts against."
        )

    design = ExperimentDesign(
        request=previous.design.request,
        base=base,
        factors=list(factors or []),
        arms=list(arms or []),
        evidence=list(evidence),
        # The previous plate (well assignments and run order) is carried forward when no format is
        # passed;
        # a randomised order is not recoverable. Passing `plate_format` is what asks for a new
        # layout.
        layout=previous.design.layout,
    )
    if plate_format:
        design = design.model_copy(
            update={
                "layout": _layout(
                    design,
                    plate_format=plate_format,
                    randomize=randomize_run_order,
                    seed=seed,
                )
            }
        )

    checks = run_checks(
        design,
        failures=await recorded_failures(design),
        precedent=await uncited_precedent(design),
    )
    if failed := blockers(checks):
        raise ChemclawError(
            "this design is not storable yet — "
            + "; ".join(f"{c.check_id}: {c.detail}" for c in failed)
        )

    changed = diff_designs(
        previous.design,
        design,
        from_revision=previous.revision,
        to_revision=previous.revision + 1,
    ).paths
    try:
        revision = await store.append(
            design_id,
            design,
            checks,
            author_kind="agent",
            author=require_actor(),
            parent_revision=parent_revision,
            change_note=change_note,
            session_id=get_current_session_id() or "",
            correlation_id=get_current_correlation_id() or "",
            status="draft",
        )
    except RevisionConflict as exc:
        raise ChemclawError(str(exc)) from exc

    status = await _stored_status(store, design_id)
    logger.info(
        "protocol.drafted design_id=%s revision=%s arms=%d evidence=%d",
        design_id,
        revision.revision,
        len(design.arms),
        len(design.evidence),
    )
    return _readable(
        receipt(
            design,
            checks,
            design_id=design_id,
            revision=revision.revision,
            status=status,
            changed_paths=changed,
        )
    )


def _layout(
    design: ExperimentDesign, *, plate_format: int, randomize: bool, seed: int | None
) -> PlateLayout:
    """The plate layout for this design, translating a layout refusal into a usable message."""
    try:
        return place(design.arms, plate_format=plate_format, randomized=randomize, seed=seed)
    except LayoutError as exc:
        suggestion = smallest_plate_for(len(design.arms))
        hint = (
            f" The smallest plate that holds {len(design.arms)} arms is {suggestion}."
            if suggestion and suggestion != plate_format
            else ""
        )
        raise ChemclawError(f"{exc}.{hint}") from exc


@tool
async def read_experiment_protocol(design_id: str, revision: int = 0) -> str:
    """Read a stored design — the whole protocol, its checks and its revision history.

    Use this to reopen a design a previous turn drafted, to read what a chemist changed about it,
    or before revising one so the revision is derived from the current head rather than from your
    memory of it.

    Args:
        design_id: The `design-…` id.
        revision: A specific revision, or 0 for the current head.

    Returns:
        JSON with `receipt` (the summary and the checks), `design` (the whole document),
        `markdown` (the protocol as a chemist reads it — quote from this rather than rebuilding
        it) and `run_sheet` (the path a chemist downloads the plate from as a CSV — give them this
        link rather than retyping the table, and never edit the path you are handed).

    Raises:
        ChemclawError: no design or no such revision.
    """
    store = _store()
    stored = await _read_design_or_refuse(store, design_id, revision)
    body = ProtocolReadout(
        receipt=receipt(
            stored.design,
            stored.checks,
            design_id=design_id,
            revision=stored.revision,
            status=await _stored_status(store, design_id),
        ),
        design=stored.design,
        markdown=render_markdown(stored.design, stored.checks),
        run_sheet=run_sheet_path(design_id, stored.revision),
    )
    return _readable(body)


class RescaleReadout(BaseModel):
    """A rescaled protocol, the factor, and what the rescale refused to touch.

    `caveats` is not a footnote and is deliberately a first-class field beside `design`: a reader
    that reports the scaled charges without them has produced exactly the document that makes a
    scaled batch fail. `protocols/rescale.py` says why each entry is on the list.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    design_id: str = ""
    from_revision: int = 0
    factor: float = 0.0
    basis: str = ""
    design: ExperimentDesign
    caveats: list[dict[str, str]] = Field(default_factory=list)
    stored: bool = False


# The tool description is short because it is in every call's prefix
# (`tests/test_context_floor.py`). The scaling rationale lives in `protocols/rescale.py` and
# `skills/protocol-scale-translation`; the description keeps what is needed to call it and the rule
# that caveats are reported, not summarised.
@tool
async def rescale_experiment_protocol(design_id: str, target_scale: str) -> str:
    """Scale a stored protocol's charges to a new basis, and list what does not scale.

    Charges move by one factor off the limiting line; equivalents do not. Stores nothing — to keep
    it, call `draft_experiment_protocol` with the head's `parent_revision`.

    Args:
        design_id: The `design-…` id to scale.
        target_scale: The new basis as a quantity — "2 kg", "500 mL". Must be in the dimension the
            limiting charge line already states.

    Returns:
        JSON with `factor`, `basis`, the scaled `design`, and `caveats` — the quantities that did
        not scale. **Report every caveat beside the charges**; scaled charges without them are the
        document that makes a batch fail.

    Raises:
        ChemclawError: unknown design, not exactly one limiting line, a limiting line with no
            amount, or a target needing a molar mass or density.
    """
    store = _store()
    stored = await _read_design_or_refuse(store, design_id)
    try:
        result = rescale(stored.design, target=target_scale)
    except RescaleError as exc:
        raise ChemclawError(str(exc)) from exc
    return _readable(
        RescaleReadout(
            design_id=design_id,
            from_revision=stored.revision,
            factor=result.factor,
            basis=result.basis,
            design=result.design,
            caveats=[
                {"where": c.where, "quantity": c.quantity, "reason": c.reason}
                for c in result.caveats
            ],
        )
    )


class PlateReadout(BaseModel):
    """A design's outcomes, plus the observations they make for a campaign when one is named."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcomes: PlateOutcomes
    # Each measured arm's factor levels beside its value — the shape a campaign fits. Empty unless
    # an outcome was named, because "every outcome at once" is not a table a surrogate can take.
    observations: list[dict[str, float | str]] = Field(default_factory=list)
    # Why `observations` is empty although an outcome was named: its latest values span more than
    # one
    # unit. Reported beside the readout, which the chemist needs to fix that.
    observations_refused: str = ""


class AttachedResults(BaseModel):
    """What landed, and what the plate now looks like as a whole."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    design_id: str = ""
    revision: int = 0
    attached: int = 0
    arms_without_results: list[str] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)


# The description is short because it is charged to every model call; the judgment about reporting
# results lives in `skills/hte-campaign-design`.
@tool
async def attach_plate_results(
    design_id: str,
    results: list[ArmResult],
    revision: int = 0,
) -> str:
    """Attach measured outcomes to the arms of a stored design.

    Closes the loop a designed plate otherwise leaves open: the arms this records become the
    observations `suggest_next_experiment` can fit. Append-only — a re-measured well is a second
    observation, not a correction, and both stay visible.

    Args:
        design_id: The `design-…` the plate was laid out as.
        results: One entry per measured well: `arm_id`, `outcome` (use the design's own
            `analytics.measures` wording), `value`, and optionally `unit`, `reaction_id`,
            `measured_at`, `note`.
        revision: The revision the plate was **run from**, or 0 for the head. Pass the printed
            revision when it is not the head; a later edit must not re-point these numbers.

    Returns:
        JSON with `attached`, `arms_without_results` (named, not counted) and `disagreements`.
        **Report the unmeasured arms**: a summary of only what landed makes a half-run plate look
        finished.

    Raises:
        ChemclawError: No such design or revision, an `arm_id` that revision does not have, or a
            design belonging to another chemist.
    """
    store = _store()
    # The results table is append-only and this tool is its only writer, so an unowned write here
    # could never be taken back: the same `owner_permits` rule its two sibling writers apply.
    await _require_writable(store, design_id)
    stored = await _read_design_or_refuse(store, design_id, revision)
    parsed = [ArmResult.model_validate(result) for result in results]
    try:
        require_arms_exist(stored.design, parsed)
    except UnknownArm as exc:
        raise ChemclawError(str(exc)) from exc
    results_store = default_arm_result_store()
    attached = await results_store.append(
        design_id,
        stored.revision,
        parsed,
        author_kind="agent",
        author=require_actor(),
    )
    outcomes = summarise(
        stored.design,
        design_id,
        stored.revision,
        await results_store.read(design_id, stored.revision),
    )
    return _readable(
        AttachedResults(
            design_id=design_id,
            revision=stored.revision,
            attached=attached,
            arms_without_results=outcomes.arms_without_results,
            disagreements=outcomes.disagreements,
        )
    )


@tool
async def read_plate_results(design_id: str, outcome: str = "", revision: int = 0) -> str:
    """Read a design's measured outcomes, and the observations they make for a campaign.

    Args:
        design_id: The `design-…` to read.
        outcome: Name one to also get `observations` — each measured arm's factor levels beside its
            value, which is the shape `suggest_next_experiment` fits a surrogate to.
        revision: A specific revision, or 0 for the head.

    Returns:
        JSON with `results`, `arms_without_results`, `disagreements`, and `observations` when an
        outcome was named. An arm with no measurement is **omitted** from observations rather than
        defaulted: a missing well is not a zero. Values in mixed units give no observations and
        an `observations_refused` naming the arms under each unit.

    Raises:
        ChemclawError: No such design or revision.
    """
    store = _store()
    stored = await _read_design_or_refuse(store, design_id, revision)
    rows = await default_arm_result_store().read(design_id, stored.revision)
    observations: list[dict[str, float | str]] = []
    refused = ""
    if outcome:
        try:
            observations = observations_for(stored.design, outcome, rows)
        except MixedUnits as exc:
            refused = str(exc)
    return _readable(
        PlateReadout(
            outcomes=summarise(stored.design, design_id, stored.revision, rows),
            observations=observations,
            observations_refused=refused,
        )
    )


class ProtocolListing(BaseModel):
    """A page of designs, **and how many designs that page is a page of**.

    `protocols.render.DesignListing` — a bare `designs` list — is what this replaced, and it had
    the silence `GET /sessions` in the same tree was explicitly fixed for. Driven: 60 designs
    stored, the shipped default returned 20, and the payload's only key was `designs`, so the
    answer "here are the stored experiment designs" was written over a third of them.
    """

    designs: list[DesignSummary] = Field(default_factory=list)
    # Everything matching the same filters, before the page bound.
    total: int = Field(default=0, ge=0)
    # The bound actually applied, which is not always the one asked for.
    limit_applied: int = Field(default=0, ge=0)

    # `frozen` but not `extra="forbid"`: the serialized `computed_field` could not be validated back
    # under `forbid`.
    model_config = ConfigDict(frozen=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """The one sentence to read before saying what designs exist.

        A `computed_field` so the sentence is serialized with the listing.
        """
        if self.total > len(self.designs):
            return (
                f"PARTIAL: {len(self.designs)} of {self.total} matching designs are shown, most "
                f"recently updated first (page bound {self.limit_applied}). Older ones exist — "
                "narrow with `status`/`project` or raise `limit` before saying what has been run."
            )
        if not self.designs:
            return (
                "NONE: no stored design matches these filters. Nothing has been designed here yet, "
                "or the filters exclude it."
            )
        return (
            "COMPLETE: every design matching these filters is shown, most recently updated first."
        )


@tool
async def find_experiment_protocols(status: str = "", project: str = "", limit: int = 20) -> str:
    """List stored experiment designs, newest first.

    Args:
        status: `requested`, `draft`, `approved`, `executed` or `abandoned`. Empty for all.
        project: Narrow to one project.
        limit: How many, up to 50 (see `limit_applied`).

    Returns:
        JSON: `designs`, a page of `{design_id, title, mode, status, project, head_revision, arms,
        blockers, updated_at}`, plus `total` and a `verdict` — a short page is not a short corpus.
    """
    allowed = {"requested", "draft", "approved", "executed", "abandoned"}
    if status and status not in allowed:
        raise ChemclawError(f"unknown status {status!r}; one of {', '.join(sorted(allowed))}")
    index = await _store().listing(
        status=status or None,  # type: ignore[arg-type]
        project=project,
        limit=max(1, min(limit, _LISTING_LIMIT)),
    )
    return _readable(
        ProtocolListing(designs=index.designs, total=index.total, limit_applied=index.limit_applied)
    )


class ExperimentArms(BaseModel):
    """A campaign's suggested points as the factors and arms a protocol is drafted from."""

    campaign_id: str
    objective: str
    factors: list[Factor]
    arms: list[ProtocolArm]
    constants: dict[str, str]
    #: What the translation could not supply, one sentence each — units above all. Read these
    #: before drafting; none of them is optional and none is checked downstream.
    notes: list[str]


# The description is short because a tool docstring is sent on every model call
# (D-2026-09-14-a-docstring-is-a-prompt-and-a-comment-is-not). This tool turns BO suggestions into
# labelled factor levels and arms so the model does not transcribe a candidate table by hand;
# `protocols/from_bo.py` carries the full rationale.
@tool
async def experiment_arms_from_campaign(campaign_id: str, prefix: str = "arm") -> str:
    """Turn a campaign's latest suggestion into the factors and arms to draft a protocol from.

    Use this between `suggest_next_experiment` and `draft_experiment_protocol` instead of reading
    the candidate table and writing the arms out yourself. It gives you which parameters the runs
    vary, the settings they take, and which runs repeat each other. It does **not** give you the
    protocol body — the charge table, steps, analytics and hazards are yours to write.

    **Read `notes` before drafting.** An optimisation problem carries no units, so a temperature
    factor comes back with none and no check downstream will catch it. `notes` also names the
    parameters that are the same in every run: those are setpoints for the body, not factors, and
    they are not in the arms.

    Args:
        campaign_id: The campaign, as `suggest_next_experiment` returned it. The id is a hash of
            the decision space, so one whose space has since changed will not resolve — ask for a
            fresh suggestion instead.
        prefix: The arm-id stem. Use another when adding a second block of arms to one design.

    Returns:
        The objective, the factors and arms, the parameters held constant, and what you must still
        supply.

    Raises:
        ChemclawError: The campaign has suggested nothing yet, or its points and its decision space
            disagree. Retrying fixes neither.
    """
    thread = await read_campaign_thread(campaign_id)
    if not thread.last_candidates:
        raise ChemclawError(
            f"campaign {campaign_id!r} has no recorded suggestion yet, so there are no points to "
            "turn into arms. Call `suggest_next_experiment` for this campaign first."
        )
    translated = await asyncio.to_thread(
        factors_and_arms,
        thread.problem,
        [candidate.params for candidate in thread.last_candidates],
        prefix=prefix,
    )
    return _readable(
        ExperimentArms(
            campaign_id=thread.campaign_id,
            objective=thread.objective,
            factors=translated.factors,
            arms=translated.arms,
            constants=translated.constants,
            notes=translated.notes,
        )
    )
