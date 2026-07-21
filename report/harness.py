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

from chemclaw.ids import stable_hash
from kg.note import Note
from report.evidence import EvidenceChunk, SourceRetriever

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
    """A section after retrieval: its cited evidence, and whether any was found."""

    heading: str
    memory_layer: str
    evidence: list[EvidenceChunk]

    @property
    def supported(self) -> bool:
        """True iff at least one evidence chunk backs this section."""
        return bool(self.evidence)


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
    """
    lines = [f"# {report.title}\n"]
    for section in report.sections:
        lines.append(f"## {section.heading} [layer: {section.memory_layer}]\n")
        if not section.supported:
            lines.append("_No supporting data found; section left unsupported._\n")
            continue
        for chunk in section.evidence:
            lines.append(f"- {chunk.content} ([[{chunk.source_note_id}]], via {chunk.retriever})")
        lines.append("")
    return Note(
        id=_report_id(report.title),
        type="report",
        created_by="agent",
        source="report:development-report",
        body="\n".join(lines) + "\n",
    )
