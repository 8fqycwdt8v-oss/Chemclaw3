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
from chemclaw.kg.note import Note, cited_ids, safe_identifier
from chemclaw.retrieval.evidence import EvidenceChunk, SourceRetriever

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
    """A report to draft: a title and the sections to research."""

    title: str = Field(min_length=1)
    sections: list[ReportSection] = Field(min_length=1)


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


def _citation(source_note_id: str) -> str:
    """Render a chunk's source as a wikilink when it *is* a note id, and as plain text otherwise.

    Not every retriever returns one. `ingest/eln/warehouse/retriever` yields `<source>:<row key>`
    and `ingest/sources/vendored_dataset` yields `vendored:<dataset>:<index>` — both shipped, both
    correct as provenance and neither a note. Wrapping those in `[[…]]` produced two failures at
    once: the colon makes the reader parse the prefix as a **relation**, so `[[eln-snowflake:12]]`
    becomes a `eln-snowflake` edge to a note called `12`, and `kg-validate` then refuses the report
    for an unknown relation type — a draft nobody can merge, naming an edge nobody wrote.

    The predicate is the *reader's*, deliberately: a target is safe to link exactly when the
    citation parser gives it back unchanged. Inventing a slug pattern here would be a second
    definition of "note id" to drift against `chemclaw.kg.note`, and this one cannot — if the link
    syntax ever grows a new form, the writer follows the reader for free.

    A non-note source stays visible rather than being dropped: it is what the section rests on, and
    a reader who cannot see it cannot check it — but it is reduced to an identifier charset first.
    **The plain-text branch was itself a forgery vector**, and the identical one this file's own
    sibling change closed for ELN bodies: a retriever key is warehouse data, and
    `x]] and [[supersedes:reaction-eln-0001]] [[z` reaches the body verbatim, where
    `Note.outgoing_links` reads the `[[supersedes:…]]` in the middle of it as a real edge. So the
    generated report proposes retiring another team's result, through the branch added to *avoid*
    writing a link.

    `safe_identifier`, not an escape, because this branch is already saying "this is not a note
    id" — a provenance label only has to be recognisable, and the charset that makes it so is one
    an instruction (or a wikilink) cannot survive.
    """
    return (
        f"[[{source_note_id}]]"
        if cited_ids(f"[[{source_note_id}]]") == [source_note_id]
        else f"source {safe_identifier(source_note_id)}"
    )


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
    """
    evidence: list[EvidenceChunk] = []
    for retriever in retrievers:
        evidence.extend(await retriever.retrieve(section.query, section.filters))
    return SynthesizedSection(
        heading=section.heading, memory_layer=section.memory_layer, evidence=evidence
    )


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
    known = {chunk.source_note_id for chunk in evidence}
    supported: list[Claim] = []
    discarded: list[Claim] = []
    for claim in claims:
        if claim.citations and all(citation in known for citation in claim.citations):
            supported.append(claim)
        else:
            discarded.append(claim)
    return supported, discarded


def report_note(report: Report) -> Note:
    """Render the report as a PR-gated `report` note citing every source (5b.7).

    Each section shows its memory layer and lists its evidence, every chunk wikilinking its
    source note; an unsupported section says so explicitly. The draft is agent-authored and
    goes through the PR-gate for a chemist to validate before it counts as reliable (D-005).

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
        if section.retrieval_failed:
            # A failed section is flagged distinctly from an empty one: the gap is visible to the
            # reviewer (and re-runnable), never silently absent from the draft (F10-D2).
            lines.append("_Retrieval failed for this section; incomplete — re-run required._\n")
            continue
        if not section.supported:
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
            lines.append(f"- {chunk.content} ({', '.join(provenance)})")
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
