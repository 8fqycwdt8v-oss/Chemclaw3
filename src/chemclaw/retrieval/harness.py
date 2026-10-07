"""Source-agnostic report harness core.

Pure orchestration over the `SourceRetriever` contract. `gather_section` collects a section's
cited evidence (one durable unit of the report workflow); a section with none is marked
**unsupported**, never invented. `verify_claims` drops any synthesized claim that cites nothing
or cites a note that was not retrieved. `report_note` renders the draft as a `report` note that
cites every source and declares each section's memory layer.
"""

import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from chemclaw.core.ids import stable_hash
from chemclaw.kg.note import Note, as_cell, require_note_slug, split_link
from chemclaw.retrieval.evidence import EvidenceChunk, SourceRetriever
from chemclaw.retrieval.fanout import sweep_sources

# Each section declares which memory layer it draws on, so the report keeps evidenced history
# (episodic) and transferred generalization (semantic) structurally apart, not just by prose.
MemoryLayer = Literal["evidence", "episodic", "semantic"]


class ReportSection(BaseModel):
    """One requested section: its heading, the query to answer, and its memory layer."""

    heading: str = Field(min_length=1)
    query: str = Field(min_length=1)
    memory_layer: MemoryLayer
    filters: dict[str, Any] = Field(default_factory=dict)


class ReportRequest(BaseModel):
    """A report to draft: a title, the sections to research, and who asked for it."""

    title: str = Field(min_length=1)
    sections: list[ReportSection] = Field(min_length=1)
    # Who asked. Required: entitlement-gated sources need the actor (or they decline and the draft
    # reads as a complete sweep), and the run's logs must join back to the requester.
    requested_by: str = Field(min_length=1)
    # Captured at launch: roles come from the validated token for the turn, and a background run has
    # no turn to look them up in.
    requested_roles: list[str] = Field(default_factory=list)
    # The turn that asked, so logs and the report note join back to it; `durable/interceptor.py`
    # binds it by field name. Empty outside a turn rather than invented. Not part of `_report_id`,
    # which must not fork a new research run per re-asking turn.
    correlation_id: str = ""
    # The conversation that asked, so the draft also lands there as a `document` artefact. Empty
    # outside a session (and for histories started before the field, which keeps their command
    # sequence unchanged). Not part of `_report_id`, for `correlation_id`'s reason.
    session_id: str = ""


class SectionRequest(BaseModel):
    """One section to retrieve, plus the identity to retrieve it as.

    A pair rather than a field on `ReportSection`, because who asked is a property of the *run* and
    not of the section: the same section spec is re-usable across runs with different requesters.

    **Both halves of the original sentence here were falsified by later work in the same campaign**
    and are corrected rather than left: `ReportRequest.requested_by` is now `min_length=1`, so a
    request without a requester is rejected instead of being the ordinary scheduled case, and
    `chemclaw.agent.durable_tools._report_id` — the *workflow* id — *does* key on the actor: it had
    to, because two principals with different entitlements were colliding on one id and one of them
    collected the other's report. That is a different function in a different module from the
    `_report_id` sixty lines below this one, which mints a *note* id from the title alone; the
    unqualified name read as a security claim about the wrong one.

    It exists at all because the fan-out addresses each child workflow by its argument, so an
    identity that stops at the parent never reaches the activity that does the retrieving — which is
    exactly where an entitlement is checked.

    **`requested_by` stays optional here while `ReportRequest.requested_by` is `min_length=1`, and
    that pair is deliberate rather than the drift it looks like.** The two are validated at
    different places for different failures. `ReportRequest` is the front door: it is constructed
    once, by `request_development_report`, from `require_actor()`, and rejecting an unattributed
    request there costs a caller an error message. `SectionRequest` is a *derived* payload that
    crosses the durable boundary — it is serialized into workflow history and deserialized later,
    possibly by a differently-versioned worker. A `min_length=1` there turns a payload the workflow
    already accepted into an activity that fails identically on every retry, and history is
    immutable, so the run cannot be repaired: a front-door constraint is a rejection, the same
    constraint on a replayed payload is a wedge.

    What makes the laxness safe is that the absent case fails *closed*. `retrieve_section` stamps no
    identity when there is no requester, so an entitlement-gated source correctly contributes
    nothing; the widening direction — a run reading more than its requester may — is unreachable
    because the only constructor of this type passes a `ReportRequest`'s validated actor through.
    """

    section: "ReportSection"
    requested_by: str = ""
    requested_roles: list[str] = Field(default_factory=list)
    # Relayed from `ReportRequest.correlation_id` so `retrieve_section`'s log lines are joinable.
    # Lax so a replayed payload without it degrades to an unjoined line rather than wedging.
    correlation_id: str = ""


class SynthesizedSection(BaseModel):
    """A section after retrieval: its cited evidence, and whether retrieval succeeded.

    `retrieval_failed` distinguishes "retrieval errored (this section is incomplete)" from the
    ordinary "retrieval ran and found nothing" — a distinction the chemist reading the report must
    see, since a durable report must never let a failed section masquerade as a genuinely empty one
    (F10-D2). It stays False on every success path.

    **`failed_sources` and `skipped_sources` are here because one bool could not carry two
    remedies.** `sweep_sources` returns the two separately — a source that *raised* and a source
    that *declined* — and `gather_section` collapsed both into `retrieval_failed`, so a vector
    index throwing `ConnectionError` and a share leg declining because the actor is unentitled
    rendered the identical sentence:

        B. vector raised (ConnectionError)   → "_Some retrieval sources failed …re-run required._"
        C. share declined (unentitled)       → "_Some retrieval sources failed …re-run required._"

    That the section is incomplete either way is not in question and `gather_section` argues it
    correctly. What was wrong is the *rendered remedy*: re-running C as the same actor produces
    the same section forever, so the report told a chemist to do the one thing that cannot work.

    Both default empty, which is what keeps `durable/report_workflow.py`'s constructions — a
    section whose whole activity failed, where there is no per-source detail to have — rendering
    exactly as they did.
    """

    heading: str
    memory_layer: str
    evidence: list[EvidenceChunk]
    retrieval_failed: bool = False
    #: The sources that raised, by name. A re-run can fix these.
    failed_sources: list[str] = Field(default_factory=list)
    #: The sources that declined, mapped to the reason each stated. A re-run cannot fix these.
    skipped_sources: dict[str, str] = Field(default_factory=dict)

    @property
    def supported(self) -> bool:
        """True iff retrieval succeeded and at least one evidence chunk backs this section."""
        return not self.retrieval_failed and bool(self.evidence)


class Report(BaseModel):
    """A drafted report: the title and its synthesized, cited sections."""

    title: str
    sections: list[SynthesizedSection]


class Claim(BaseModel):
    """A synthesized statement and the source notes it claims to rest on."""

    text: str = Field(min_length=1)
    citations: list[str]


def _report_id(title: str) -> str:
    """A ref-safe, unique note id from a report title.

    Slugged to `[a-z0-9-]` (a valid file path) with a short hash of the exact title, so titles that
    slug alike stay distinct.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    digest = stable_hash(title, chars=8)
    return f"report-{slug}-{digest}" if slug else f"report-{digest}"


async def gather_section(
    section: ReportSection, retrievers: list[SourceRetriever]
) -> SynthesizedSection:
    """Fan one section's query out to every retriever and collect its cited evidence.

    A section nothing answers is kept but empty (`supported` is False) and rendered unsupported.
    This is the durable unit of the report workflow: one section, one activity.

    Goes through `sweep_sources`, the conversational path's fan-out, so a dead source costs only its
    own leg and the healthy legs' evidence is kept. Lists are deduplicated on `(note, content)`
    (as `_interleave_dedup` does) so a note several legs return is cited once, without re-ranking.
    `retrieval_failed` is set by any failed source, so an incomplete sweep never reads as empty.
    """
    ranked_lists, failed, skipped = await sweep_sources(
        [(retriever.name, retriever) for retriever in retrievers],
        section.query,
        section.filters,
    )
    seen: set[tuple[str, str]] = set()
    evidence: list[EvidenceChunk] = []
    for chunks in ranked_lists:
        for chunk in chunks:
            key = (chunk.source_note_id, chunk.content)
            if key in seen:
                continue
            seen.add(key)
            evidence.append(chunk)
    # A skip counts as incompleteness here: a signed report swept without a source covers less than
    # the whole corpus, whatever the reason.
    return SynthesizedSection(
        heading=section.heading,
        memory_layer=section.memory_layer,
        evidence=evidence,
        retrieval_failed=bool(failed or skipped),
        # Carried apart: a raise and a decline need different remedies (see `SynthesizedSection`).
        failed_sources=list(failed),
        skipped_sources=dict(skipped),
    )


def groundable_ids(evidence: list[EvidenceChunk]) -> set[str]:
    """Every id a citation may ground against: each chunk's source id, plus its colon-split half.

    Document chunks carry `<retriever>:<doc>#<ordinal>`, and `cited_ids` splits a wikilink at the
    first colon, so the stored id's split half is what a citation arrives as. Note ids contain no
    colon, so they are unchanged.
    """
    ids = {chunk.source_note_id for chunk in evidence}
    return ids | {split_link(stored)[1] for stored in ids}


def verify_claims(
    claims: list[Claim], evidence: list[EvidenceChunk]
) -> tuple[list[Claim], list[Claim]]:
    """Split claims into (supported, discarded) against the retrieved evidence.

    A claim is supported only if it cites at least one note and every cited note was retrieved; an
    uncited or fabricated-citation claim is discarded, not softened. The `development-report` skill
    runs this over each synthesized claim, so the guard lives in tested code.
    """
    known = groundable_ids(evidence)
    supported: list[Claim] = []
    discarded: list[Claim] = []
    for claim in claims:
        if claim.citations and all(citation in known for citation in claim.citations):
            supported.append(claim)
        else:
            discarded.append(claim)
    return supported, discarded


def _gap_notices(section: SynthesizedSection, *, whole: bool) -> list[str]:
    """The sentence(s) naming what this section was swept without, and what to do about it.

    Two causes, two remedies: a source that raised is transient (re-run); a source that declined
    will decline again (re-running cannot help). Sources are named so a reader knows where to go.
    The legacy wording is kept for a section whose whole activity failed, which has no per-source
    detail.

    Args:
        section: The section being rendered, carrying its own failed and skipped source names.
        whole: Whether *nothing* was retrieved, which changes only the clause about the evidence.

    Returns:
        One markdown line per distinct cause, each ending in a newline.
    """
    if not section.failed_sources and not section.skipped_sources:
        if whole:
            return ["_Retrieval failed for this section; incomplete — re-run required._\n"]
        return [
            "_Some retrieval sources failed for this section; the evidence below is "
            "incomplete — re-run required._\n"
        ]
    # "Retrieval failed" stays the failure sentence's opening (what readers grep for); the skip
    # sentence deliberately opens differently.
    tail = "this section is incomplete" if whole else "the evidence below is incomplete"
    notices = []
    if section.failed_sources:
        named = ", ".join(sorted(section.failed_sources))
        notices.append(
            f"_Retrieval failed for {named} in this section; {tail} — re-run required._\n"
        )
    if section.skipped_sources:
        named = "; ".join(
            f"{name}: {reason}" for name, reason in sorted(section.skipped_sources.items())
        )
        notices.append(
            f"_Retrieval was declined for this section ({named}); {tail}. Re-running as the same "
            "actor will produce the same section — the entitlement or the filters must change._\n"
        )
    return notices


def _as_evidence(content: str) -> str:
    """One chunk's text, unable to add structure to the report it is placed in.

    `kg.note.as_cell` strips wikilinks (so retrieved text cannot mint edges on the note) and
    collapses whitespace (so a multi-line excerpt stays one bullet instead of rendering as extra
    evidence). The text is preserved, not truncated.
    """
    return as_cell(content)


def _citation(source_note_id: str) -> str:
    """How a chunk's source is cited: a wikilink for a note, a code span for anything else.

    Wikilinking a non-note id would mint a dangling edge (and `sharedrive:sop-7#0` parses as a typed
    edge), so other addresses are kept verbatim as literal text.
    """
    try:
        require_note_slug(source_note_id)
    except ValueError:
        return f"`{source_note_id}`"
    return f"[[{source_note_id}]]"


def report_note(report: Report, *, drafted_on: date | None = None) -> Note:
    """Render the report as a `report` note citing every source.

    Each section shows its memory layer and lists its evidence; an unsupported section says so. The
    draft is agent-authored and every claim sits beside its citation.

    A chunk fills one bullet and may not add a bullet or a citation: each is placed as a cell
    (`_as_evidence`) and cited as what it is (`_citation`). A bullet also shows the provenance that
    is informative when set (a conflict, a stated confidence, an agent-authored source), since a
    report is where two agreeing-looking bullets are most likely read as independent confirmation.

    `drafted_on` becomes `valid_from`, so the note reaches standing digest queries (an absent
    `valid_from` reads as open-ended, not news). It defaults to `None` rather than today because
    this runs in workflow code, where a wall-clock read breaks replay; `_report_id` keys on the
    title alone, so both calls agree on the id.
    """
    lines = [f"# {report.title}\n"]
    for section in report.sections:
        lines.append(f"## {section.heading} [layer: {section.memory_layer}]\n")
        if section.retrieval_failed and section.evidence:
            # A partially failed section keeps what was retrieved: the marker shows the gap, and the
            # evidence
            # renders under it.
            lines.extend(_gap_notices(section, whole=False))
        elif section.retrieval_failed:
            # Nothing was retrieved at all: flagged distinctly from an empty section, so the gap is
            # visible to
            # the reader (and re-runnable), never silently absent.
            lines.extend(_gap_notices(section, whole=True))
            continue
        elif not section.supported:
            lines.append("_No supporting data found; section left unsupported._\n")
            continue
        for chunk in section.evidence:
            provenance = [_citation(chunk.source_note_id), f"via {chunk.retriever}"]
            if chunk.created_by == "agent":
                # The only thing telling a reader which source notes are this system's own
                # paraphrase.
                provenance.append("agent-authored")
            if chunk.confidence is not None:
                # Stated uncertainty, as opposed to the default; ranking lower is not the same as
                # being told.
                provenance.append(f"confidence {chunk.confidence:.2f}")
            lines.append(f"- {_as_evidence(chunk.content)} ({', '.join(provenance)})")
            if chunk.conflicts_with:
                # Conflicting ids stay plain text (the report warns about them, it does not rest on
                # them), and are
                # named as the strongest when there are more, with the count.
                hidden = chunk.conflicts_total - len(chunk.conflicts_with)
                scope = f" (the {len(chunk.conflicts_with)} strongest of "
                scope = f"{scope}{chunk.conflicts_total})" if hidden > 0 else ""
                lines.append(
                    f"  - **Conflicts with {', '.join(chunk.conflicts_with)}**{scope} — these "
                    "notes disagree; do not read this and a conflicting note as two independent "
                    "confirmations."
                )
        lines.append("")
    return Note(
        id=_report_id(report.title),
        type="report",
        created_by="agent",
        source="report:development-report",
        body="\n".join(lines) + "\n",
        valid_from=drafted_on,
    )
