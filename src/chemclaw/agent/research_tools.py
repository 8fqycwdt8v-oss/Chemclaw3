"""The agent's cross-source evidence gatherer.

`gather_evidence` sweeps every internal source behind the `SourceRetriever` contract (the
knowledge graph's notes, plus reaction-fingerprint search when an anchor is given) and returns
cited evidence in one call. Adding a source is a registry entry, not a change here. Every chunk
carries its note id so the agent can cite it and `expand_note` it.

The judgment (decomposing the question, separating evidence from analogy) lives in the
`deep-research` skill; this tool only gathers.
"""

import logging
from datetime import date
from itertools import zip_longest
from typing import Any, Literal

from pydantic import Field, computed_field

from chemclaw.agent.framing import defang, frame_untrusted
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.tool_registry import tool
from chemclaw.ingest.eln.records import default_record_store
from chemclaw.ingest.rejections import IngestRejection, refusals_matching
from chemclaw.ingest.sources.registry import active_retrieve_corpora, active_retrieve_sources
from chemclaw.retrieval.evidence import EvidenceChunk, EvidenceSweep, Hits, SourceRetriever
from chemclaw.retrieval.fanout import record_kept_chunks, sweep_sources
from chemclaw.retrieval.hybrid import (
    reciprocal_rank_fusion,
    restated_as_position,
    with_no_leg_cut_out,
)
from chemclaw.retrieval.retrievers import FingerprintReactionRetriever
from chemclaw.science.fingerprints.store import default_reaction_store

logger = logging.getLogger(__name__)

# Test seam: swap the production reaction store for an in-memory one without a database.
_reaction_store = default_reaction_store

# Which bound cut the sweep, or `None` when nothing did.
_Truncation = Literal["count", "chars"] | None


class EvidenceSweepWithRefusals(EvidenceSweep):
    """A sweep, plus the records an ingest source offered and this system refused.

    **The two halves are different kinds of statement and the type keeps them apart.** A chunk is
    evidence, cited to a note a reader can expand; a refusal is a fact about data that is *not*
    there, and the corpus holds nothing to cite for it. Folding a rejection into `chunks` — as a
    retriever returning `EvidenceChunk`s would have — is exactly the confusion this whole ledger
    exists to prevent: the well logged at 119.43% is the one entry of the seeded corpus that can
    never arrive, and reporting its refusal as a hit would hand a chemist a yield the system
    refused to believe.

    Subclassing rather than widening `EvidenceSweep` because `retrieval/` is the source-agnostic
    retriever contract and a rejection comes from no retriever. The composition belongs to the tool
    that answers the chemist's question, which is here.
    """

    # Refusals whose id or reason matches the question. The field name matters: a pydantic tool
    # return reaches the model as its `repr`.
    refused_on_ingest: list[IngestRejection] = Field(default_factory=list)
    # How many refusals matched, which exceeds `len(refused_on_ingest)` once the ledger's
    # `_MAX_MATCHES` prompt budget bites — so the cut is never silent.
    refusals_total: int = Field(default=0, ge=0)
    # Why the rejection ledger could not be asked; empty when it was. An unreachable ledger and a
    # clean corpus must not render alike — the same rule `sources_failed` exists for one field up.
    refusals_unavailable: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def disputed(self) -> str:
        """What a chunk's `conflicts_with` means, in the payload, when there is one to read.

        Nothing else in the prompt explains the marker, and a computed field costs nothing in the
        tool-schema prefix: it appears only when there is a disagreement to report. The sentence is
        `retrieval/harness.py`'s, verbatim, so the two renderings cannot drift; the added part says
        the disputing notes may have been cut and are reachable only by `expand_note`.
        """
        marked = [chunk for chunk in self.chunks if chunk.conflicts_with]
        if not marked:
            return ""
        ids = sorted({note for chunk in marked for note in chunk.conflicts_with})
        return (
            f"DISPUTED: {len(marked)} of these chunks come from a note that other notes disagree "
            f"with ({', '.join(ids)}) — these notes disagree; do not read this and a conflicting "
            "note as two independent confirmations. A disputing note is often not in this sweep "
            "at all, because the cap cut it: expand_note each id in a chunk's conflicts_with "
            "before you rest an answer on that chunk, and say the claim is disputed either way."
        )


def _text_retrievers() -> list[SourceRetriever]:
    """The active retrieve halves from the data-source registry.

    Adding a text source is a registry entry plus a config token; the default (`graph`) yields
    the single `GraphRetriever`.
    """
    return list(active_retrieve_sources())


def _sources(reaction_smiles: str | None) -> list[tuple[str, SourceRetriever]]:
    """Every source this sweep asks, named, in the order the merge downstream expects.

    The name is the retriever's own, because it labels the per-source counter and stream event
    and must match the `retriever` field on the returned chunks. The fingerprint retriever is
    last and present only when a `reaction_smiles` anchor was given, so the text sources' order
    is stable either way.
    """
    sources: list[tuple[str, SourceRetriever]] = [
        (retriever.name, retriever) for retriever in _text_retrievers()
    ]
    if reaction_smiles is not None:
        # The anchor, not the query: this source searches structures, so the anchor is bound into
        # the retriever and the fan-out stays uniform.
        anchored = _AnchoredRetriever(_reaction_store(), reaction_smiles)
        sources.append((anchored.name, anchored))
    return sources


class _AnchoredRetriever:
    """A fingerprint retriever that answers the structural anchor rather than the text query.

    The fan-out asks every source one question; binding the anchor here keeps that uniform and
    keeps the substitution in the one place that knows why the questions differ.
    """

    # The inner retriever's name: the chunks carry `retriever="reaction-fingerprint"`, and that
    # string keys `settings.retrieval_source_weights` and labels the branch's counter.
    name = FingerprintReactionRetriever.name

    def __init__(self, store: Any, reaction_smiles: str) -> None:
        """Bind the store and the anchor this retriever will answer with."""
        self._inner = FingerprintReactionRetriever(store, default_record_store())
        self._anchor = reaction_smiles

    async def retrieve(self, _query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Search structures for the bound anchor, ignoring the sweep's text query.

        The filters are forwarded, so the inner retriever's date window and eligibility gate apply.
        """
        return await self._inner.retrieve(self._anchor, filters)


def _interleave_dedup(ranked_lists: list[list[EvidenceChunk]]) -> list[EvidenceChunk]:
    """Round-robin the per-source hit-lists into one, dropping exact (note, content) repeats.

    The `graph` mode's cross-source merge. Rank position is comparable across sources and score
    is not (confidence, `ts_rank`, cosine and Tanimoto are different scales), so each source
    contributes its best hit before any contributes its second, and a source that runs out
    stops taking slots. This keeps the cap in `gather_evidence` from starving a source.

    Dedup granularity is a contract: this mode keys on `(note, content)`, so two excerpts of one
    note are two chunks, while `hybrid`'s RRF keys on the note id. Switching `retrieval_mode`
    changes chunk counts as well as order.
    """
    seen: set[tuple[str, str]] = set()
    merged: list[EvidenceChunk] = []
    for position in zip_longest(*ranked_lists):
        for chunk in position:
            if chunk is None:  # this source has no hit at this depth
                continue
            key = (chunk.source_note_id, chunk.content)
            if key not in seen:
                seen.add(key)
                merged.append(chunk)
    return merged


async def _refused_on_ingest(query: str) -> tuple[list[IngestRejection], int, str]:
    """The refused records this question matches, how many matched, and any read failure.

    The failure string keeps an unreachable ledger distinguishable from a clean corpus.

    `reason` is externally authored (it is `str(exc)` over a record an ELN export wrote), so it
    is framed with `frame_untrusted`, which tells the model to read it as data. `source` and
    `entry_id` are labels that must stay citeable, so they are only `defang`ed against a forged
    envelope delimiter. Framing does not change what a rejection is: `kind="ingest-rejection"`
    and the `refused-on-ingest:` envelope id say no note exists to expand.
    """
    try:
        found = await refusals_matching(query)
    except Exception as exc:
        # An unreachable ledger costs this footnote and nothing else; it is reported in the return
        # value, never swallowed into an empty list.
        logger.warning("ingest rejection ledger could not be read: %s", exc)
        return [], 0, f"the ingest rejection ledger could not be read ({type(exc).__name__})"
    return (
        [
            rejection.model_copy(
                update={
                    # The content channel, framed as data. The id names the ledger row rather than a
                    # note, because the record is absent.
                    "reason": frame_untrusted(
                        rejection.reason,
                        note_id=f"refused-on-ingest:{rejection.source}:{rejection.entry_id}",
                    ),
                    # The label channels: neutralised, not wrapped.
                    "entry_id": defang(rejection.entry_id),
                    "source": defang(rejection.source),
                }
            )
            for rejection in found.rejections
        ],
        found.total_matching,
        "",
    )


def _as_date(value: str, field: str) -> date:
    """Parse an ISO date argument, or fail with a message the model can act on.

    Naming the field and the expected format lets the model's next attempt be correct.
    """
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD), got {value!r}") from exc


@tool
async def gather_evidence(
    query: str,
    reaction_smiles: str | None = None,
    note_type: str | None = None,
    tag: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> EvidenceSweepWithRefusals:
    """Gather cited evidence from this deployment's own records; there is no literature index.

    Runs each text source on `query`, and — when an anchor reaction is given — also pulls
    structurally similar past reactions (DRFP). Results are merged and de-duplicated. Empty is a
    valid answer (nothing on file, not nothing published) and never invented: if every source is
    unreachable this raises rather than returning empty, so an outage is never reported as an
    absence.

    Args:
        query: The natural-language question or key terms (matched over note id/tags/body).
        reaction_smiles: Optional `reactants>>products` anchor to also pull similar reactions.
        note_type: Optional graph filter, e.g. "reaction", "optimization-campaign", "playbook".
        tag: Optional graph tag filter (e.g. a project name).
        since: Optional ISO date (YYYY-MM-DD); keep only notes dated on or after it — for a
            reaction note, the day it was run. A note with no date is excluded, not assumed in
            range. Structural hits from a `reaction_smiles` anchor are windowed too.
        until: Optional ISO date (YYYY-MM-DD); keep only notes dated on or before it.

    Returns:
        The sweep: its `chunks` (each with its content, the `source_note_id` to cite or expand, and
        which retriever found it), plus what it could not say. **Read `truncated_by`,
        `sources_truncated`, `sources_failed` and `refused_on_ingest` before concluding anything
        from an absence.**
        `truncated_by` cuts the *merged* list — `count` means narrow with a `note_type`/`tag`/date
        filter, `chars` means the chunks are long and a narrower question reaches further — and
        `total_before_cap` says how much survived merging. **`None` there does not mean you saw
        everything**: each source also cuts before the merge, and `sources_truncated` says which
        did and by how much. `{"graph": 4992}` means you are reading the top eight of thousands —
        narrow the question, or say the answer is from the best matches rather than the whole
        record. A source absent from it cut nothing or cannot tell. A name in `sources_failed`
        could not be asked at all, so the answer covers less than the corpus however complete the
        chunks look.

        `refused_on_ingest` is **not evidence and not a result**. Each entry is a record an ingest
        source offered and this system *refused*, with the reason, so it is absent from the corpus
        however well it matches. Say that ("rejected on ingest because …"); never present its id,
        its numbers or its reason as something found in the corpus, and never fill the gap it names
        with a value. The list is capped and `refusals_total` is how many matched;
        `refusals_unavailable` is non-empty when the ledger could not be asked at all.
    """
    filters: dict[str, Any] = {}
    if note_type is not None:
        filters["type"] = note_type
    if tag is not None:
        filters["tag"] = tag
    if since is not None:
        filters["since"] = _as_date(since, "since")
    if until is not None:
        filters["until"] = _as_date(until, "until")

    # One ordered hit-list per source, each ranked best-first by its retriever.
    #
    # Swept as a `Send` fan-out (`chemclaw.retrieval.fanout`) so each branch reports what it
    # contributed, distinguishing a source that returned nothing from one nobody asked. The fan-in
    # preserves source order, which both merge modes depend on for a deterministic result.
    sources = _sources(reaction_smiles)
    ranked_lists, failed, skipped = await sweep_sources(sources, query, filters)
    if failed and len(failed) == len(sources):
        # Every source was unreachable, so `[]` would falsely mean "nothing on file". Raise so the
        # model reports the failure. A partial failure costs only its own source and is visible in
        # the stream and in `sources`/`sources_skipped`.
        raise ChemclawError(
            f"evidence sources unavailable: {', '.join(sorted(failed))}. No source could be "
            f"queried, so this is not an answer about what the knowledge base contains."
        )

    # `hybrid` fuses the per-source rankings; `graph` (the default) round-robins them. Both are
    # cross-source-fair under the cap below.
    if settings.retrieval_mode == "hybrid":
        # RRF's order is the order the cap keeps. `corpora` makes the fusion one-corpus-one-vote:
        # `graph`, `lexical` and `vector` rank one note tree, and RRF assumes independent rankers.
        corpus_of = active_retrieve_corpora()
        merged = reciprocal_rank_fusion(
            ranked_lists,
            k=settings.retrieval_fusion_k,
            weights=settings.retrieval_source_weights_map,
            corpora=[corpus_of.get(name, name) for name, _ in sources],
        )
        # The cut below is a prefix of this order, and a weight can make that prefix one leg, so
        # `with_no_leg_cut_out` keeps each leg's best chunk. Round-robin gives the same guarantee by
        # construction.
        merged = with_no_leg_cut_out(
            merged, ranked_lists, limit=settings.gather_evidence_max_chunks
        )
    else:
        # Round-robin, not a union re-sorted by score: scores from different retrievers are not
        # comparable (see `_interleave_dedup`).
        merged = _interleave_dedup(ranked_lists)
    # Re-state `score` as the merged position in both modes, so the number agrees with the
    # delivered order. The note's confidence stays in `confidence`, and `retriever` names the
    # leg that found it.
    ranked = restated_as_position(merged)
    # Frame each chunk's content as retrieved data, so adversarial note text is read as evidence,
    # not an instruction. `source` is a second retrieved-text channel (warehouse row keys reach
    # it), so it is `defang`ed — a label must stay readable, so it is not framed.
    framed = [
        chunk.model_copy(
            update={
                "content": frame_untrusted(chunk.content, note_id=chunk.source_note_id),
                "source": defang(chunk.source),
                # `source_note_id` also carries a warehouse row key and is serialized to the model,
                # so it is defanged too (not `safe_id`'d: a citation must stay resolvable).
                "source_note_id": defang(chunk.source_note_id),
            }
        )
        for chunk in ranked
    ]
    kept, truncated_by = _within_budget(framed)
    # Map back to the unframed chunks for attribution: framing rewrites both halves of the dedup
    # key, so attribution must use the vocabulary the merge deduped in.
    origins = {id(copy): original for copy, original in zip(framed, ranked, strict=True)}
    kept_origins = [origins[id(chunk)] for chunk in kept if id(chunk) in origins]
    # Record what each leg kept after the merge and the budget, beside what it handed over.
    # Every source asked is passed so a starved leg reads as zero, and what each leg offered is
    # passed because `chunk.retriever` names only the leg that found a note first.
    record_kept_chunks(
        kept_origins, {name: hits for (name, _), hits in zip(sources, ranked_lists, strict=True)}
    )
    # Counted before the refusals are read, deliberately: a rejection is not a retrieved chunk and
    # must not enter the accounting a starved-source alert reads.
    refused, refusals_total, refusals_unavailable = await _refused_on_ingest(query)
    return EvidenceSweepWithRefusals(
        chunks=kept,
        refused_on_ingest=refused,
        refusals_total=refusals_total,
        refusals_unavailable=refusals_unavailable,
        truncated_by=truncated_by,
        total_before_cap=len(framed),
        sources_failed=sorted(failed),
        # Pre-merge counts, so a source out-competed at the cap still shows it was asked. The skip
        # reasons are the retrievers' own words (`RetrieverSkip`).
        sources={name: len(hits) for (name, _), hits in zip(sources, ranked_lists, strict=True)},
        # The per-leg cut, non-zero entries only; an absence means "not truncated, or not knowable".
        sources_truncated={
            name: hits.dropped
            for (name, _), hits in zip(sources, ranked_lists, strict=True)
            if isinstance(hits, Hits) and hits.dropped
        },
        sources_skipped=skipped,
    )


def _within_budget(chunks: list[EvidenceChunk]) -> tuple[list[EvidenceChunk], _Truncation]:
    """Spend both budgets down the merged ranking, and say which one ran out.

    Both a chunk count and a character budget apply, because chunk sizes differ widely across
    sources and a count alone bounds nothing. Walking the already cross-source-fair ranking in
    order keeps the character cut fair too. What is charged is the serialized chunk, not just
    its content, since the metadata fields reach the model as well. At least one chunk always
    survives, because an empty result would read as "nothing on file".
    """
    budget = settings.gather_evidence_max_chars
    kept: list[EvidenceChunk] = []
    spent = 0
    for chunk in chunks[: settings.gather_evidence_max_chunks]:
        # One extra serialization for at most `gather_evidence_max_chunks` chunks, which buys the
        # only number that means anything here: what this chunk actually costs the context window.
        cost = len(chunk.model_dump_json())
        if kept and spent + cost > budget:
            return kept, "chars"
        kept.append(chunk)
        spent += cost
    if len(chunks) > len(kept):
        return kept, "count"
    return kept, None
