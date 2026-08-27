"""Source-agnostic report harness core (plan steps 5b.1, 5b.4, 5b.7).

Pure orchestration over the `SourceRetriever` contract — it knows no concrete source (G6).
`gather_section` fans a section's query out to every retriever and collects cited evidence
(the durable unit of the report workflow); a section with no evidence is marked
**unsupported**, never filled with invention. `verify_claims`
is the adversarial gate (5b.4): a synthesized claim survives only if it cites evidence that was
actually retrieved — an uncited or fabricated-citation claim (the "invented statistic") is
dropped. `report_note` renders the draft as a PR-gated `report` note that cites every source and
declares each section's memory layer, so evidenced and analogical content stay structurally
separated (5b.5).
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from chemclaw.core.ids import stable_hash
from chemclaw.kg.note import Note, require_note_slug, split_link, strip_links
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
    # Who asked. This was the one user-launched durable job whose input dropped the actor entirely —
    # `ConnectorJobInput.requested_by` and `TemplateRunInput.requested_by` are both `min_length=1`,
    # while `request_development_report` called `require_actor()` and discarded the result.
    #
    # Two things followed. An entitlement-gated source contributes nothing to the report, because
    # `ShareDocumentRetriever._entitled()` correctly declines when no identity is set — and
    # `gather_section` only concatenates, so an un-entitled source is indistinguishable from one
    # with no matches and `retrieval_failed` stays False. The chemist gets a draft that reads as a
    # complete sweep of every internal source. And the PR-gated draft is proposed unattributed, so
    # it does not appear in the requester's own review queue.
    #
    # `min_length=1`, matching `ConnectorJobInput.requested_by` and `TemplateRunInput.requested_by`.
    # An earlier version left it optional to keep "a scheduled report" expressible — but there is no
    # scheduled-report launcher: `request_development_report` is the only constructor in `src/`, and
    # `require_actor()` either raises or returns `settings.service_actor_id`, never `""`. The
    # optional field bought nothing real and permitted a future caller to launch a report with no
    # attribution, which is the failure the other two inputs' `min_length=1` exists to prevent.
    requested_by: str = Field(min_length=1)
    # Captured at launch rather than looked up at retrieval time, because there is no directory to
    # look an actor's roles up in — the front door gets them from the validated token and they live
    # in a contextvar for the turn. A background run has no turn, so if the roles do not travel on
    # the request they do not exist by the time an entitlement is checked.
    requested_roles: list[str] = Field(default_factory=list)


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


class SynthesizedSection(BaseModel):
    """A section after retrieval: its cited evidence, and whether retrieval succeeded.

    `retrieval_failed` distinguishes "retrieval errored (this section is incomplete)" from the
    ordinary "retrieval ran and found nothing" — a distinction a chemist signing the report at the
    PR-gate must see, since a durable report must never let a failed section masquerade as a
    genuinely empty one (F10-D2). It stays False on every success path.
    """

    heading: str
    memory_layer: str
    evidence: list[EvidenceChunk]
    retrieval_failed: bool = False

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

    The title is slugged to `[a-z0-9-]` only (so the id is a valid git branch and file path —
    a raw title with `/`, `:`, etc. would break `GitNoteSubmitter`), and a short hash of the
    exact title is appended so distinct titles that slug alike (case/punctuation) stay unique.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    digest = stable_hash(title, chars=8)
    return f"report-{slug}-{digest}" if slug else f"report-{digest}"


async def gather_section(
    section: ReportSection, retrievers: list[SourceRetriever]
) -> SynthesizedSection:
    """Fan one section's query out to every retriever and collect its cited evidence.

    A section whose query returns nothing from any retriever is kept but empty
    (`supported` is False) — the report will mark it unsupported rather than invent content.
    This is the durable unit of the report workflow (5b.6): one section, one activity.

    **Through `sweep_sources`, the same fan-out the conversational path uses, because there is one
    question here and there should be one implementation of asking it.** This was a second
    `asyncio.gather` over the identical retriever set, and the two copies had drifted into opposite
    error semantics. `fanout._sweep` catches per branch, so a dead source costs its own leg; this
    one passed no `return_exceptions`, so the first raising retriever propagated out, failed the
    whole activity, burned its `BAD_DATA_RETRY` budget, and left the section rendering as
    "*Retrieval failed for this section*" — **discarding the evidence the healthy sources had
    already found**. One dead share threw away three working sources' work.

    The shared sweep also brings what this path never had: a per-source counter, a stream event, and
    the `failed` channel that says which sources could not be asked. Concurrency is preserved (it
    was the reason the original `gather` existed, and `sweep_sources` fans out the same way), and so
    is argument order, which the fusion downstream treats as load-bearing.

    `retrieval_failed` is now set from *any* failed source rather than from an exception escaping.
    That is the honest reading of what the flag documents — a section a chemist signs must not let
    an incomplete sweep pass as a genuinely empty one — and it is strictly more informative than
    before, because the section is marked incomplete *and* keeps what was retrieved.
    """
    ranked_lists, failed, skipped = await sweep_sources(
        [(retriever.name, retriever) for retriever in retrievers],
        section.query,
        section.filters,
    )
    evidence = [chunk for chunks in ranked_lists for chunk in chunks]
    # A skip counts as incompleteness here, deliberately: for the conversational sweep a declined
    # source is an answer the model can relay, but a *report* is signed by a chemist, and a
    # section swept without the share leg (an unentitled service actor, a filter the source
    # cannot serve) is a section about less than the whole corpus, whatever the reason.
    return SynthesizedSection(
        heading=section.heading,
        memory_layer=section.memory_layer,
        evidence=evidence,
        retrieval_failed=bool(failed or skipped),
    )


def groundable_ids(evidence: list[EvidenceChunk]) -> set[str]:
    """Every id a citation may ground against: each chunk's source id, plus its colon-split half.

    `source_note_id` is not always a note id: document chunks carry `<retriever>:<doc>#<ordinal>`
    (`retrievers._document_chunks` explains the shape). A wikilink citing one —
    `[[docs:abc123#4]]` — is partitioned by `cited_ids` at the first colon into a relation and an
    id, so the citation arrives as `abc123#4` and could never equal the stored id: measured, every
    document citation in an answer scored as ungrounded while the note-id citations beside it
    passed. Adding each stored id's own split half makes the two extractions meet in the middle
    without special-casing any retriever's naming, and note ids are unchanged — a slug cannot
    contain a colon, so its split half is itself.
    """
    ids = {chunk.source_note_id for chunk in evidence}
    return ids | {split_link(stored)[1] for stored in ids}


def verify_claims(
    claims: list[Claim], evidence: list[EvidenceChunk]
) -> tuple[list[Claim], list[Claim]]:
    """Split claims into (supported, discarded) against the retrieved evidence (5b.4).

    A claim is supported only if it cites at least one source note and *every* cited note
    was actually retrieved. An uncited claim or one citing a note not in the evidence — a
    fabricated statistic — is discarded, not softened.

    This is the gate the `development-report` skill runs over each prose claim it synthesizes
    from the gathered evidence (5b.4): `gather_section` returns evidence chunks that are cited
    by construction, but LLM-written *claims about* that evidence are only trustworthy once
    checked here, which is why the guard lives in code, tested, not left to the prose step.
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


def _as_evidence(content: str) -> str:
    """One chunk's text, unable to add structure to the report it is placed in.

    Two rules, each closing one way content became markup. `strip_links` reduces a `[[wikilink]]`
    to its target, so text written by whoever wrote a share document cannot become an edge on a
    note this system asks a chemist to merge — `retrievers._excerpt` already did this for the three
    note-backed retrievers, and the two sources whose content is *not* a note body never passed
    through it. Collapsing whitespace runs is what keeps a chunk one bullet: a newline ends a
    Markdown line and a leading `- ` starts a list item, so a multi-line excerpt did not render
    badly, it rendered as *more evidence*.

    The text is preserved rather than truncated or dropped: a reader still sees what the source
    said, on one line, followed by the provenance that is actually its own.
    """
    return " ".join(strip_links(content).split())


def _citation(source_note_id: str) -> str:
    """How a chunk's source is cited: a wikilink for a note, a code span for anything else.

    `[[…]]` is the graph's citation syntax and `kg.graph` reads it as one, so wikilinking an id
    that is not a note id mints an edge to a note that does not exist — a dangling link that fails
    `kg-validate` and makes the report's own pull request unmergeable. Worse than unmergeable, it
    is wrong in a way a reviewer cannot see: `sharedrive:sop-7#0` parses through `split_link` as a
    *typed* edge (`relation="share"`, id `"sop-7#0"`) rather than as the address of a document.

    A share chunk, a warehouse row and a vendored record all cite something a reader can still
    check — that is `EvidenceChunk`'s contract — so the address is kept verbatim and rendered as
    literal text instead of as a link.
    """
    try:
        require_note_slug(source_note_id)
    except ValueError:
        return f"`{source_note_id}`"
    return f"[[{source_note_id}]]"


def report_note(report: Report) -> Note:
    """Render the report as a PR-gated `report` note citing every source (5b.7).

    Each section shows its memory layer and lists its evidence, every chunk wikilinking its
    source note; an unsupported section says so explicitly. The draft is agent-authored and
    goes through the PR-gate for a chemist to validate before it counts as reliable (D-005).

    **A chunk fills a bullet; it may not add one, and it may not add a citation.** Content reaches
    here as raw retrieved text — a note body's first `note_excerpt_chars`, or up to a share
    binding's `chunk_chars` of whatever a document said — and this body becomes a note a human
    merges. Interpolated verbatim, every embedded newline started a new Markdown line and every
    embedded `- ` started a new bullet, with the provenance suffix landing only on the excerpt's
    *last* line: measured on the committed corpus, eight retrieved chunks rendered as twenty-three
    bullets, fifteen of them note frontmatter reading as independent, uncited evidence. A document
    carrying `[[playbook-degassing]]` did worse than mislead a reader — it put a real outgoing edge
    on the PR-gated draft, citing a note no retriever returned. So each chunk is placed as a *cell*
    (`_as_evidence`), the same rule and for the same reason as
    `memory.comparison._placeable`, and cited as what it is (`_citation`).

    A bullet also carries the provenance its chunk *actually* holds, but only where that
    provenance is informative — a conflict, a stated confidence, an agent-authored source note.
    The three fields were populated by the retrievers (D-160) and dropped here, and dropping
    `conflicts_with` was the expensive one: `kg.conflicts` exists precisely so retrieval flags
    notes that disagree instead of returning both silently, and a report is the one output where
    two agreeing-looking bullets are most likely to be read as two independent confirmations.
    The rest is rendered only when set, because a line of empty metadata under every bullet
    would bury the one bullet that carries a warning.
    """
    lines = [f"# {report.title}\n"]
    for section in report.sections:
        lines.append(f"## {section.heading} [layer: {section.memory_layer}]\n")
        if section.retrieval_failed and section.evidence:
            # A partially-failed section keeps what was retrieved. `retrieval_failed` is set by
            # *any* failed source, and `gather_section` was changed specifically so a dead share
            # no longer throws away three working sources' chunks — but this renderer used to
            # `continue` past `section.evidence` on the flag, restoring the original defect one
            # layer down: the rendered note the chemist signed was byte-identical to the pre-fix
            # behaviour the gather docstring said was repaired. The marker stays (the reviewer
            # must see the gap), and the evidence renders under it.
            lines.append(
                "_Some retrieval sources failed for this section; the evidence below is "
                "incomplete — re-run required._\n"
            )
        elif section.retrieval_failed:
            # Nothing was retrieved at all: flagged distinctly from an empty section, so the gap
            # is visible to the reviewer (and re-runnable), never silently absent (F10-D2).
            lines.append("_Retrieval failed for this section; incomplete — re-run required._\n")
            continue
        elif not section.supported:
            lines.append("_No supporting data found; section left unsupported._\n")
            continue
        for chunk in section.evidence:
            provenance = [_citation(chunk.source_note_id), f"via {chunk.retriever}"]
            if chunk.created_by == "agent":
                # "How much of this was AI-drafted?" — a distilled agent note and a human-merged
                # one are indistinguishable in the body text, and only one of them was signed off
                # on its own merits at the PR-gate.
                provenance.append("agent-authored")
            if chunk.confidence is not None:
                # Stated uncertainty, as opposed to the unset default. This number already reached
                # the chunk as `score`, where it only orders truncation — being ranked lower is not
                # the same as the reader being *told* the note is unsure.
                provenance.append(f"confidence {chunk.confidence:.2f}")
            lines.append(f"- {_as_evidence(chunk.content)} ({', '.join(provenance)})")
            if chunk.conflicts_with:
                # The conflicting ids stay plain text, not `[[wikilinks]]`: the report *warns
                # about* those notes, it does not rest on them, and linking would add them to the
                # report's own citations — the same reason `_excerpt` strips a note's links.
                # Named as the *strongest* when there are more, because a reader shown three ids
                # and no count would reasonably conclude there were three (`conflict_max_per_note`).
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
    )
