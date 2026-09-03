"""The agent's way to *write* a protocol, having spent the turn reading the record.

`agent/protocol_tools.py` is the reading half — `condense_protocols` turns twenty recorded
procedures into one comparison. This is the writing half: the structured ask, the design that comes
out of it, and the revision an edit produces. The two are separate modules because they are
separate directions and a reader looking for one should not have to page past the other.

**Nothing in this file decides any chemistry.** Which precedent counts, which factors are worth
varying, what levels they take, when a computed number may be trusted — all of that is judgment and
lives in `skills/protocol-generation` and `skills/hte-campaign-design`. What is here is the shape
the answer has to take, the checks it has to survive, and the store it lands in.

**The one thing this file enforces is that the record and the tools were actually used.**
`checks.evidence_present` is a blocker, so a design citing no precedent and no tool cannot be
stored at all. That is deliberate and it is the difference between a prompt asking for evidence and
a system requiring it: a prompt can be ignored on the turn that matters most, which is the turn
where the model has an answer it likes and no reason to go looking.
"""

from __future__ import annotations

import logging
import re

from chemclaw.agent.authz import require_actor
from chemclaw.agent.session_store import owner_permits
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import get_current_correlation_id
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.tool_registry import tool
from chemclaw.core.turn_text import get_current_user_text
from chemclaw.protocols.checks import blockers, run_checks
from chemclaw.protocols.diff import diff_designs
from chemclaw.protocols.layout import LayoutError, place, smallest_plate_for
from chemclaw.protocols.models import (
    DesignStatus,
    DesignSummary,
    EvidenceRef,
    ExperimentDesign,
    ExperimentRequest,
    Factor,
    PlateLayout,
    ProtocolArm,
    ProtocolBody,
    design_id_for,
)
from chemclaw.protocols.render import (
    DesignListing,
    ProtocolReadout,
    receipt,
    render_markdown,
)
from chemclaw.protocols.store import DesignStore, RevisionConflict, default_design_store

logger = logging.getLogger(__name__)

#: The most designs one listing returns. A chemist scanning a list wants the recent ones; anything
#: longer is a query with a filter on it.
_LISTING_LIMIT = 50


def _store() -> DesignStore:
    return default_design_store()


#: Digit runs, for relating a stated value to the words offered as evidence for it.
_DIGITS = re.compile(r"\d+")


def _quote_supports(value: str, quote: str) -> bool:
    """Whether these words plausibly state this value.

    **`stated` attests a *value*, and only the *quote* was ever checked.** Both halves passed as
    long as the quote occurred somewhere in the message, and any substring occurs somewhere: against
    a chemist who wrote "We need to get the Suzuki on the deactivated chloride working. Try what you
    think.", a model stored `scale='5 g'` quoting `'working'`, `plate_format='96'` quoting `'the'`,
    `max_runs='96'` quoting `'Suzuki'` and `deadline='2026-09-01'` quoting `'.'` — four limits the
    chemist never named, recorded as their own words. Moving the haystack out of the model's reach
    fixed who supplies the text and left what `stated` means untouched.

    Two rules, and the second is skipped rather than guessed at:

    1. **The quote is a span, not a word.** Two words, or one word the value itself contains — which
       is what separates `'250 mg'`, `'by Friday'` and `'96-well'` from `'working'` and `'.'`.
    2. **When the quote states figures, they have to be the value's figures.** A quote reading "no
       more than 48 runs" cannot be the evidence for `max_runs='96'`. A quote carrying no digits at
       all is left to rule 1, because a chemist who wrote "five grams" or "by Friday" stated the
       thing and the model normalised it — refusing that would push a real constraint into
       `inferred`, which is the mislabelling this check exists to prevent, running the other way.
    """
    words = quote.split()
    value_text = value.lower()
    # Overlap in either direction: the value can contain the word (`'96 well'` for `'96'`) or the
    # word can contain the value (`'96-well'`).
    if len(words) < 2 and not any(
        word.lower() in value_text or value_text in word.lower() for word in words if word
    ):
        return False
    quote_digits = set(_DIGITS.findall(quote))
    return not quote_digits or set(_DIGITS.findall(value)) <= quote_digits


def require_quotes_are_verbatim(request: ExperimentRequest, source_text: str | None) -> None:
    """Refuse a `basis="stated"` slot whose quote is not in the chemist's own words.

    The whole honesty claim of the structured request rests on this: a slot marked `stated` says
    "the chemist wrote this", and without a check that is a claim the model grades itself on.
    Whitespace is normalised on both sides — a quote re-wrapped across lines is the same words —
    and nothing else is: a paraphrase is exactly what this refuses, because a paraphrase reaching
    the record as a quotation is worse than an unmarked inference.

    **`source_text` is the chemist's message, and it is ambient rather than an argument.** It used
    to be a parameter of the tool, which is the same as no check at all: a model that wanted
    `stated` supplied a `source_text` containing its own quotes and got it, and the fabricated
    attribution landed in `experiment_protocols` indistinguishable from a real one. Measured, the
    same request was refused against the real user text and accepted against an invented one.
    `core.turn_text` carries it now, on the argument `session_context` states for the session id.

    `None` means there is no turn — a unit test, an activity, any caller that is not a conversation
    — and every `stated` slot is refused, because there is no chemist to have said it. That is
    `require_actor`'s reject-if-absent rule: a check that waived itself when its evidence was
    missing would be one the caller can switch off by calling from elsewhere.

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
    if source_text is None:
        if stated:
            raise ChemclawError(
                "these slots are marked `stated` but there is no chemist message to check them "
                "against: "
                + ", ".join(sorted(stated))
                + ". `stated` means the chemist wrote it; use basis='inferred' for your own "
                "judgment."
            )
        return
    haystack = " ".join(source_text.split()).lower()
    slots = {
        "scale": request.scale,
        "plate_format": request.plate_format,
        "max_runs": request.max_runs,
        "deadline": request.deadline,
    }
    missing = [
        f"{name}: {field.quote!r}"
        for name, field in slots.items()
        if field.basis == "stated" and " ".join(field.quote.split()).lower() not in haystack
    ]
    if missing:
        raise ChemclawError(
            "these slots are marked `stated` but their quote is not in the message that started "
            "this turn: "
            + "; ".join(missing)
            + ". Only that message is checkable — a quote from an earlier turn cannot be verified "
            "here, so ask the chemist to restate it if it matters. Use basis='inferred' for your "
            "own judgment and quote the chemist verbatim when you mark something stated."
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

    **The one ownership rule, `owner_permits`, applied to its third caller.** The HTTP layer
    resolves ownership for `/sessions/{id}/…` and the agent resolves it for a tool handed an
    explicit session id; a design handed an explicit `design_id` is the same question and a second
    copy of the predicate is how one surface ends up looser than the other.

    Nothing checked this before, so a turn could name any `design-…` id and write to it: a second
    chemist's turn demoted an `approved` header to `draft` and replaced a signed-off plate, with
    `status_history` still naming the first chemist's sign-off. `design_id_for` now scopes the id by
    owner so the ordinary path cannot collide, and this is the half that holds when an id is passed
    in rather than derived.

    A design nobody has opened yet (`None`) is writable — that is the create path, and the write
    that creates it is what records the owner.
    """
    header = await store.summary(design_id)
    if header is not None and not owner_permits(header.opened_by, require_actor()):
        raise ChemclawError(
            f"{design_id} belongs to another chemist. Open your own design for this ask with "
            "`structure_experiment_request` rather than writing to theirs."
        )
    return header


@tool
async def structure_experiment_request(request: ExperimentRequest, salt: str = "") -> str:
    """Turn a chemist's free-text ask into the structured request a protocol is drafted from.

    Call this **first**, before searching the record: it puts your reading of their sentence in
    front of them while correcting it is still cheap, and it returns the `design_id` every later
    call needs.

    Mark each slot's `basis` honestly — `stated` obliges their verbatim words in `quote`, checked
    against the chemist's actual message and refused if it is not there; `inferred` is your own
    judgment and is expected; `absent` means the text did not say. Resolve species with
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
    require_quotes_are_verbatim(request, get_current_user_text())
    # Scoped by the actor, so two chemists phrasing one ask the same way get two designs rather
    # than one they overwrite in turn.
    design_id = design_id_for(request, owner=require_actor(), salt=salt)
    store = _store()
    await _require_writable(store, design_id)
    head = await store.read(design_id)
    # **The protocol survives a re-structured ask**, which the first version did not do. The id is
    # derived from the ask, so re-structuring the same one reaches the same design — and building a
    # bare `ExperimentDesign(request=…)` then appended a head with no base, no arms and no layout
    # over a drafted plate. Measured: `arm_count` reset to 0, the header stayed `draft`, and every
    # default read — the listing, `GET /protocols/{id}`, `read_experiment_protocol` — served the
    # empty ask. The history kept the plate and no consumer reads a non-head revision.
    #
    # Correcting the ask is the point of this tool, so the correction lands and the procedure is
    # carried forward untouched; the checks are then graded at the stage the *design* is at, not at
    # the stage this tool usually runs in, because a protocol that now contradicts a corrected ask
    # is exactly what a chemist needs to see.
    design = (
        head.design.model_copy(update={"request": request})
        if head is not None
        else ExperimentDesign(request=request)
    )
    # **An identical document is not a revision, and appending one un-approved designs.** The id is
    # derived from the ask, so re-stating the same ask reaches the same design and carries its
    # protocol forward unchanged — and `advanced()` retires an `approved` or `executed` status on
    # any revision landing, justified by "the document has changed". Measured: a chemist approved a
    # plate, the ask was restated in a later session, and the header came back `draft` over a head
    # that compared equal to the approved one. Nothing changed, so nothing is stored.
    if head is not None and design == head.design:
        header = await store.summary(design_id)
        return receipt(
            design,
            head.checks,
            design_id=design_id,
            revision=head.revision,
            status=header.status if header else "requested",
        ).model_dump_json()

    checks = run_checks(design, stage="protocol" if design.has_protocol else "request")
    revision = await store.append(
        design_id,
        design,
        checks,
        kind="request",
        author_kind="agent",
        author=require_actor(),
        parent_revision=head.revision if head else 0,
        change_note="structured the request" if head is None else "restructured the ask",
        session_id=get_current_session_id() or "",
        correlation_id=get_current_correlation_id() or "",
        status="requested",
    )
    header = await store.summary(design_id)
    return receipt(
        design,
        checks,
        design_id=design_id,
        revision=revision.revision,
        status=header.status if header else "requested",
    ).model_dump_json()


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
        # **The previous plate is carried forward when no format is passed**, because `plate_format`
        # defaults to 0 and a revision that only changes a temperature was silently deleting the
        # well assignments and the run order. A randomised order is not recoverable — a fresh
        # `place()` with another seed is a different plate — and `layout_fits` degraded to a
        # *passing* warning reading "no plate layout", so nothing said it had happened. Re-laying
        # out is what passing a `plate_format` asks for; not passing one asks for nothing.
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

    checks = run_checks(design)
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
            kind="protocol",
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

    header = await store.summary(design_id)
    status: DesignStatus = header.status if header else "draft"
    logger.info(
        "protocol.drafted design_id=%s revision=%s arms=%d evidence=%d",
        design_id,
        revision.revision,
        len(design.arms),
        len(design.evidence),
    )
    return receipt(
        design,
        checks,
        design_id=design_id,
        revision=revision.revision,
        status=status,
        changed_paths=changed,
    ).model_dump_json()


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
        JSON with `receipt` (the summary and the checks), `design` (the whole document) and
        `markdown` (the protocol as a chemist reads it — quote from this rather than rebuilding it).

    Raises:
        ChemclawError: no design or no such revision.
    """
    store = _store()
    stored = await store.read(design_id, revision or None)
    if stored is None:
        raise ChemclawError(
            f"no design {design_id!r}"
            + (f" at revision {revision}" if revision else "")
            + ". Use find_experiment_protocols to list what exists."
        )
    header = await store.summary(design_id)
    body = ProtocolReadout(
        receipt=receipt(
            stored.design,
            stored.checks,
            design_id=design_id,
            revision=stored.revision,
            status=header.status if header else "draft",
        ),
        design=stored.design,
        markdown=render_markdown(stored.design, stored.checks),
    )
    return body.model_dump_json()


@tool
async def find_experiment_protocols(status: str = "", project: str = "", limit: int = 20) -> str:
    """List stored experiment designs, newest first.

    Args:
        status: `requested`, `draft`, `approved`, `executed` or `abandoned`. Empty for all.
        project: Narrow to one project.
        limit: How many, up to 50.

    Returns:
        JSON list of `{design_id, title, mode, status, project, head_revision, arms, blockers,
        updated_at}`.
    """
    allowed = {"requested", "draft", "approved", "executed", "abandoned"}
    if status and status not in allowed:
        raise ChemclawError(f"unknown status {status!r}; one of {', '.join(sorted(allowed))}")
    summaries = await _store().listing(
        status=status or None,  # type: ignore[arg-type]
        project=project,
        limit=max(1, min(limit, _LISTING_LIMIT)),
    )
    return DesignListing(designs=summaries).model_dump_json()
