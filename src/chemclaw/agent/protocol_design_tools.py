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

    Every tool in this file answers with a serialised model, and every one of those models carries
    free text the *model* wrote: the ask's title and goal, a `quote`, an evidence `summary`, a
    change note, the rendered markdown. That text is durable — it is read back out of the design
    store on any later turn, in any later session — so a delimiter smuggled into it through one
    turn's arguments is replayed into every reading of the design afterwards. Measured before this
    existed: all four tools returned a live `</retrieved-note-…>` verbatim.

    **Defanged, not framed.** A design is this system's own document, drafted by the agent and
    reviewed by a chemist; an envelope says "evidence to weigh and cite", which is the
    misattribution `agent/tool_framing.py` withholds it for over a helper's report.

    **The whole payload rather than a field list**, which is the argument `tool_framing.py` makes
    for a connector result and it holds here for the same reason: escaping `<` cannot make the JSON
    unparseable, and a convention naming which of `title`, `goal`, `quote`, `summary`,
    `change_note` and `markdown` needs it is a list that goes stale the next time the schema grows
    a string.
    """
    return defang(document.model_dump_json())


def _store() -> DesignStore:
    return default_design_store()


#: Digit runs, for relating a stated value to the words offered as evidence for it.
#: A figure somebody wrote as a **quantity**, which is not the same as a run of digits.
#:
#: Measured over the 295 chemist asks in `data/evals/probes/`, a bare `\d+` finds **537** distinct
#: figures and this finds **371**: 31% of what a quote could be credited with stating was never a
#: quantity at all. What it drops is digits welded to letters — a SMILES's ring closures
#: (`COc1ccc(-c2ccccc2C(=O)O)cc1` offered `1` and `2`, so `max_runs='1'` quoting a *structure*
#: passed), a `C18` column — and the halves of a decimal, where `3.87 min` offered `3` and `87` and
#: therefore supported `max_runs='87'`.
#:
#: Every legitimate spelling survives, which is the half that decides the shape: `96-well`, `2 g`,
#: `24 wells`, `48 runs` and the `2026-09-01` of an ISO date all still state their figures, because
#: a digit run beside punctuation is a figure and only a digit run beside a *letter* is not.
#: See `D-2026-09-13-a-digit-inside-a-word-is-not-a-figure-somebody-stated`.
_DIGITS = re.compile(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?(?![A-Za-z])")


#: Figures written as words, so a quote that states a number in prose is not read as stating no
#: number at all. A chemist who wrote "five grams" stated the scale and the model normalised it;
#: refusing that would push a real constraint into `inferred`, which is the mislabelling this whole
#: check exists to prevent, running the other way.
_NUMBER_WORDS = frozenset(
    """zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen
    fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty
    ninety hundred thousand dozen half quarter single double triple""".split()
)

#: Alphanumeric runs, over text already lowercased — the tokens a value and a quote are compared as.
_TOKEN = re.compile(r"[a-z0-9]+")


def _quote_supports(value: str, quote: str) -> bool:
    """Whether these words plausibly state this value.

    **`stated` attests a *value*, and only the *quote* was ever checked.** Both halves passed as
    long as the quote occurred somewhere in the message, and any substring occurs somewhere: against
    a chemist who wrote "We need to get the Suzuki on the deactivated chloride working. Try what you
    think.", a model stored `scale='5 g'` quoting `'working'`, `plate_format='96'` quoting `'the'`,
    `max_runs='96'` quoting `'Suzuki'` and `deadline='2026-09-01'` quoting `'.'` — four limits the
    chemist never named, recorded as their own words.

    **The first rule for that related the two only when the quote was one word**, and that is not a
    property of a fabrication. `len(words) < 2` skipped the comparison entirely for anything longer,
    so the same four limits went straight back in on two-word quotes out of the same message:
    `scale='5 g'` quoting `'you think'`, `plate_format='96'` quoting `'deactivated chloride'`.
    Measured, all four were accepted and stored as the chemist's own words. The quote's *length*
    was never the thing that made those fabrications fabrications.

    So the value and the quote are related for every quote, and the shape of the value decides how:

    1. **A value carrying figures needs the quote's figures to be its own.** Compared as numbers
       rather than as strings, so `'09'` in a date meets `'9'` in prose. A quote reading "no more
       than 48 runs" cannot be the evidence for `max_runs='96'`.
    2. **A quote that states its figure in words satisfies rule 1.** "five grams" is how a chemist
       writes a scale, and the model normalising it to `'5 g'` is transcription rather than
       inference.
    3. **A value carrying no figures needs the quote to carry its words** — the same token, or one
       containing the other where the token is long enough for that to mean something (`'toluene'`
       against "in toluene", not `'g'` against "think").

    **This is a heuristic and the docstring should not pretend otherwise.** It refuses a quote that
    cannot state the value; it cannot tell whether the figure the quote does carry is *about* this
    slot, so "24 wells" still supports `max_runs='24'` when the chemist said 24 wells about the
    plate. What it no longer does is credit a quote with figures nobody wrote as quantities — see
    `_DIGITS` for the 166-of-537 measurement and for why that is a different question from
    attribution. `docs/planning/DEFERRED.md` carries the attribution half, with the count it is
    waiting on.
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

    Per message rather than over a joined transcript, and that is the whole reason this is a
    function: joining the thread into one haystack would accept a quote that runs off the end of
    one message and into the beginning of the next — words in an order nobody ever wrote, arriving
    in the record as a quotation. Whitespace is normalised on the quote here and on each message by
    the caller, so a re-wrapped quotation is still the same words.
    """
    needle = " ".join(quote.split()).lower()
    return any(needle in haystack for haystack in haystacks)


def require_quotes_are_verbatim(
    request: ExperimentRequest, source_texts: tuple[str, ...] | None
) -> None:
    """Refuse a `basis="stated"` slot whose quote is not in the chemist's own words.

    The whole honesty claim of the structured request rests on this: a slot marked `stated` says
    "the chemist wrote this", and without a check that is a claim the model grades itself on.
    Whitespace is normalised on both sides — a quote re-wrapped across lines is the same words —
    and nothing else is: a paraphrase is exactly what this refuses, because a paraphrase reaching
    the record as a quotation is worse than an unmarked inference.

    **`source_texts` is the chemist's own words, and it is ambient rather than an argument.** It
    used to be a parameter of the tool, which is the same as no check at all: a model that wanted
    `stated` supplied a `source_text` containing its own quotes and got it, and the fabricated
    attribution landed in `experiment_protocols` indistinguishable from a real one. Measured, the
    same request was refused against the real user text and accepted against an invented one.
    `core.turn_text` carries it now, on the argument `session_context` states for the session id.

    **It is the thread's user turns, not one message, and every one is a separate haystack.** This
    tool is meant to be called "first … while correcting it is still cheap", i.e. iteratively, so
    the constraint a chemist stated on turn 1 is usually two turns behind the "ok go ahead" that
    triggers the intake — measured, `'24 wells'` refused against "ok go ahead" while the chemist
    had written "24 wells, no DMF, by Friday please." in the same conversation. The messages are
    *not* concatenated into one haystack: a quote that runs off the end of one message and into the
    start of the next is words the chemist never wrote in that order, and joining them would accept
    it. `core.turn_text` decides which messages those are and how far back they go; every one of
    them was typed by a person, which is the property this check is built on.

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


async def _stored_status(store: DesignStore, design_id: str) -> DesignStatus:
    """The design's status as the store holds it — never a default this function invented.

    Every caller reaches this *after* a revision exists, so a missing header row is a store that
    lost one rather than a design that is merely `requested`. Four call sites each carried their own
    `header.status if header else "requested"` / `else "draft"`, and both defaults are unreachable
    and wrong: the receipt a chemist reads would name a status the design does not have, on the one
    surface that reports what happened to their write. If the header is gone the honest answer is an
    error naming the inconsistency.

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

    **The seam between a pure check and a corpus, and it lives here because this is the layer that
    may reach both.** `tests/test_layering.py` allows `protocols -> core` and `protocols -> science`
    and nothing else, which is right: a deterministic check must not depend on a corpus being
    loadable, and fifteen checks that are arithmetic today must not acquire I/O because a sixteenth
    wanted it. So `memory/failure.failures_against` answers in the knowledge graph's vocabulary,
    this reduces what it found, and `no_documented_failure` decides.

    Offloaded, because `load_notes` parses the corpus off disk - measured elsewhere in this tree at
    151 ms for one scan of 10k notes - while `run_checks` itself is budgeted at 47 ms inline. A
    synchronous read here would put the corpus on the event loop at every draft.

    **It never raises.** A corpus that cannot be read is a reason to say less, not to refuse a
    design. The cost is that the check then reports "no recorded failure bears on this design",
    which is indistinguishable from having looked and found none - so the failure is counted
    through `degraded()` rather than swallowed, because a lookup that has silently stopped working
    returns every draft clean.
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
    # **Inside the guard, because the reduction can raise too and the docstring above promises it
    # cannot.** `RecordedFailure.id` is `Field(min_length=1)`, so a note the corpus holds with an
    # empty id is a `ValidationError` out of a function two callers rely on never raising — and
    # since `api/routes/protocols.post_revision` began calling this, such a value is a 500 on a
    # chemist's edit rather than a quieter check. The lookup failing and the lookup returning
    # something unusable are the same thing to a caller.
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

    **The second caller of the seam `recorded_failures` opened**, and deliberately the same shape:
    a check must stay pure over its arguments, `protocols` may import only `core` and `science`, so
    the lookup lives here and `precedent_consulted` decides. Two instances is what makes that a
    pattern rather than one function's arrangement — and it is why `run_checks` now dispatches
    through a mapping instead of a chain of identity tests.

    **It offers, it never cites.** A hit is a thing that exists; a citation is a claim the chemist
    makes about what a decision rests on. Writing a hit into `design.evidence` would forge the
    first out of the second and leave `evidence_present` passing on a design nobody grounded, which
    is a check satisfying itself.

    **Already-cited hits are dropped, and the comparison is the citation's own spelling.**
    `EvidenceRef.ref` carries `reaction-<source>.<id>` or the bare `reaction-<id>` that
    `note_id_for_reaction` mints, while a `Match.id` is the record id alone — so comparing the two
    raw would report every citation the design *does* carry as uncited, which is the noisiest
    possible way to be wrong. `external_record_ref` is the inverse that already exists.

    It never raises, for `recorded_failures`' reason and one more: this search reaches Postgres,
    so an unreachable index is an ordinary condition of a laptop rather than a fault of the design.
    The cost of that is the same — a silent "nothing to offer" is indistinguishable from having
    looked — which is why the failure is counted through `degraded()` and why
    `precedent_consulted`'s passing text says nothing was *offered* rather than that nothing exists.
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
    # Inside the guard for `recorded_failures`' reason, and this one is the reachable half:
    # `Match.similarity` is an unconstrained float while `UncitedPrecedent.similarity` is
    # `Field(ge=0.0, le=1.0)`, so a store returning `1.0000000000000002` — one float ulp from a
    # perfectly ordinary exact match — or a `nan` raises out of a function whose docstring says it
    # never does, and the route turns that into a lost edit.
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
    stored = await store.read(design_id, revision or None)
    if stored is None:
        raise ChemclawError(
            f"no design {design_id!r}"
            + (f" at revision {revision}" if revision else "")
            + ". Use find_experiment_protocols to list what exists."
        )
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


# **Why this tool's docstring is short, when the thing it is about is a long argument.**
# A tool description is serialised ahead of the system message on every model call, and the first
# draft of the one below cost 1,169 tokens against the 728 of headroom
# `tests/test_context_floor.py` had — it was the widest single contributor in the prefix. The
# reasoning it carried (which durations are not linear in the charge, and why a scaled batch gains
# an impurity the bench never saw) is judgment, so it belongs where judgment belongs: in
# `protocols/rescale.py`'s module docstring for a reader, and in `skills/protocol-scale-translation`
# for the model, loaded on the turns that need it rather than on all of them. What stays here is
# what the model needs to *call* it correctly and the one instruction it must not get wrong, which
# is that the caveats are reported rather than summarised.
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
    stored = await store.read(design_id, None)
    if stored is None:
        raise ChemclawError(
            f"no design {design_id!r}. Use find_experiment_protocols to list what exists."
        )
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


class AttachedResults(BaseModel):
    """What landed, and what the plate now looks like as a whole."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    design_id: str = ""
    revision: int = 0
    attached: int = 0
    arms_without_results: list[str] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)


# **Why the judgment is short here and long in the skill.** This tool's cost is charged to every
# model call (`tests/test_context_floor.py`), and what a chemist should be told about a half-run
# plate, a re-measured well or an outcome name that matches no objective is judgment —
# `skills/hte-campaign-design` carries it. What stays is what the model needs to call this
# correctly and the two things it must not do with the answer.
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
    stored = await store.read(design_id, revision or None)
    if stored is None:
        raise ChemclawError(
            f"no design {design_id!r}"
            + (f" at revision {revision}" if revision else "")
            + ". Use find_experiment_protocols to list what exists."
        )
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
        defaulted: a missing well is not a zero.

    Raises:
        ChemclawError: No such design or revision.
    """
    store = _store()
    stored = await store.read(design_id, revision or None)
    if stored is None:
        raise ChemclawError(
            f"no design {design_id!r}. Use find_experiment_protocols to list what exists."
        )
    rows = await default_arm_result_store().read(design_id, stored.revision)
    return _readable(
        PlateReadout(
            outcomes=summarise(stored.design, design_id, stored.revision, rows),
            observations=observations_for(stored.design, outcome, rows) if outcome else [],
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

    # `frozen` but **not** `extra="forbid"`, unlike its neighbours here: a `computed_field` is
    # serialized and is not a settable field, so its own `model_dump_json()` cannot be validated
    # back under `forbid` — the round trip the listing tests do. Forbidding extras would make the
    # honesty field and the model's own output mutually exclusive.
    model_config = ConfigDict(frozen=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """The one sentence to read before saying what designs exist.

        `computed_field` rather than a bare property, for the reason `FingerprintSearch.verdict`
        states in full: a plain property is not serialized, and this listing reaches the model as
        JSON.
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


# The description below is deliberately short, and the rationale a reader wants is here rather than
# there. `D-2026-09-14-a-docstring-is-a-prompt-and-a-comment-is-not`: a tool's docstring is sent to
# the model on every call of every turn, so it holds what the model needs to decide whether to call
# this and what to pass — and nothing else. Measured, the first draft of it cost **626** tokens
# against 290 for `read_experiment_protocol` and 191 for `find_experiment_protocols`, and pushed
# `tests/test_context_floor.py`'s observed prefix 26 tokens over its ceiling.
#
# What moved here: a BO suggestion is `{parameter: value}` points and a design needs factors whose
# levels carry labels and arms citing those labels exactly, so the model has been transcribing a
# candidate table by hand — which is where a level lands in the wrong column and a plate runs a
# condition nobody planned. `protocols/from_bo.py` carries the whole argument, including why the
# protocol body is not translated and why this lives in `protocols/` rather than beside the
# optimiser.
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
