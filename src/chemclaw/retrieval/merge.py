"""How the sources' ranked hit-lists become one bounded list of evidence.

One merge and one budget, for every path that sweeps `SourceRetriever`s. There are two such paths —
`agent.research_tools.gather_evidence` answers a chemist mid-conversation and
`retrieval.harness.gather_section` researches one section of a durable, PR-gated report — and until
this module existed only the first had any of it. The second was
`[chunk for chunks in ranked_lists for chunk in chunks]`: a flat concatenation, no dedup, no cap.

**What that cost, measured on the committed 38-note corpus** with `graph,vector,lexical` and the
query "palladium catalyst yield": 24 chunks over **12** distinct notes, 7 of them returned more
than once, three notes returned three times with byte-identical content — and `report_note`
rendered all 24 as bullets. Its own docstring names that exact failure ("a report is the one output
where two agreeing-looking bullets are most likely to be read as two independent confirmations"),
so the renderer was arguing against something the gatherer was handing it, three legs at a time.
It scales as legs x `retrieval_top_k`.

**Why the merge is the same one and not a second one tuned for reports.** The question both paths
ask is identical — several sources ranked the same corpus, which chunks survive — and
`D-2026-08-01-a-cap-that-starves-a-source` is the record of what a *second*, differently-shaped cut
does to a leg. The budgets are the same settings for the same reason: a deployment that widens
`gather_evidence_max_chars` because its notes are long has widened it for the same corpus the
report reads. A report *section* is bounded exactly as a conversational sweep is, and a report is
many sections, which is the same arithmetic as a chemist asking many questions.

This module holds no retriever and reaches no store, so it costs `retrieval` nothing to import and
`agent` nothing to import from.
"""

from itertools import zip_longest
from typing import Literal

from chemclaw.core.config import settings
from chemclaw.retrieval.evidence import EvidenceChunk
from chemclaw.retrieval.hybrid import reciprocal_rank_fusion

# Which bound cut a merged list, or `None` when nothing did.
Truncation = Literal["count", "chars"] | None


def interleave_dedup(ranked_lists: list[list[EvidenceChunk]]) -> list[EvidenceChunk]:
    """Round-robin the per-source hit-lists into one, dropping exact (note, content) repeats.

    The `graph` retrieval mode's cross-source merge, and the thing that makes the character cap
    fair. **A source's rank position is comparable across sources; its score is not** —
    `EvidenceChunk.score` is a note's `confidence` from the graph, a `ts_rank` from Postgres FTS, a
    cosine from the dense index and a Tanimoto from the fingerprint store, and the chunk's own
    docstring says so. Concatenating the lists and then sorting the union by that number let one
    source's scale decide the whole sweep, and the cap then kept a prefix of whichever scale ran
    highest.

    Measured on a mixed sweep — 45 graph hits at the notes' 0.8 confidence, 8 lexical hits at
    ts_rank 0.02–0.09 and 7 dense hits at cosine 0.60–0.85, against the 40-chunk cap — the flat
    union returned 38 graph / 0 lexical / 2 vector, and with the sort taken out it returned
    40 / 0 / 0: the concatenation order alone starves the later sources, and the score sort was
    mitigating that rather than causing it. Either way the lexical leg contributed nothing an agent
    could read, which is the whole reason a deployment enables it.

    Round-robin fixes the cap instead of re-tuning the ranking. Each source's own order is
    preserved (every retriever already returns best-first), each contributes its best hit before
    any source contributes its second, and a source that runs out simply stops taking a slot — so
    the budget flows to whoever still has hits rather than being carved into fixed quotas. With a
    single source it is that source's list unchanged, which is the default deployment.

    **The two merge modes dedup at different granularities, and that is a contract, not an
    accident.** This mode keys on `(note, content)` — two different excerpts of one note are two
    pieces of evidence and both may spend a slot — while `hybrid`'s RRF keys on the note id and
    keeps one representative chunk, because rank fusion is a statement about *notes* across
    ranked lists and a per-excerpt fusion would double-count whichever note fragments most.
    Switching `retrieval_mode` therefore changes chunk counts as well as order; a reader
    comparing sweeps across modes is comparing different units, and the report layer's warning
    about "two agreeing-looking bullets" applies within one note's excerpts here.
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


def merge_ranked_lists(ranked_lists: list[list[EvidenceChunk]]) -> list[EvidenceChunk]:
    """Fuse the sources' rankings into one, the way `settings.retrieval_mode` says.

    `hybrid` fuses the per-source rankings (a note any source ranks highly rises); `graph` (the
    default) round-robins them. Both are cross-source-fair under the budget below, differing in
    whether a note found twice is *rewarded* for it. Either way graph expansion stays the
    reasoning path.

    RRF already produces the cross-source ranking (best first), so it *is* the order the cap
    keeps — re-sorting by a single source's raw score would discard the fusion. And the `graph`
    mode is a round-robin rather than a flat union re-sorted by score for the reason
    `interleave_dedup` measures: sorting the union by `score` compares a note's confidence against
    a `ts_rank` against a cosine, which is the comparison `EvidenceChunk.score` documents as
    invalid.
    """
    if settings.retrieval_mode == "hybrid":
        return reciprocal_rank_fusion(
            ranked_lists,
            k=settings.retrieval_fusion_k,
            weights=settings.retrieval_source_weights_map,
        )
    return interleave_dedup(ranked_lists)


def within_budget(chunks: list[EvidenceChunk]) -> tuple[list[EvidenceChunk], Truncation]:
    """Spend both budgets down the merged ranking, and say which one ran out.

    **Both, because either alone is unbounded in the other.** `gather_evidence_max_chunks` counts
    chunks whose sizes differ ~7.5x across sources — a note excerpt is `note_excerpt_chars` (240)
    and a share chunk is up to its binding's `chunk_chars` (1,800) — so 40 chunks is ~9.6 kB from
    the graph and ~72 kB from a share, and nothing normalised them. A count of things cannot bound
    anything, because what a thing costs is whatever is in it: exactly the finding
    `agent_keep_last_conversation_groups` records, where counting groups left a 300k-token thread
    at 180k against a 100k budget.

    **Spent by walking the merged ranking, which is what keeps it fair.**
    `D-2026-08-01-a-cap-that-starves-a-source` is about the *shape* of a cut rather than its size:
    `chunks` is already round-robin across sources (or RRF-fused), so consuming it in order spends
    the character budget cross-source-fairly for the same reason the count is. A second cap applied
    the old way — per source, or over a re-sorted union — would reintroduce the starvation that
    ADR measured to zero surviving chunks on a whole leg.

    **At least one chunk always survives.** An over-budget first chunk would otherwise return an
    empty list, which `gather_evidence`'s contract says means "nothing on file" — the same clamp
    `KeepLastConversationGroupsEdit` makes for the same reason, since an empty result that reads as
    an honest absence is worse than an oversized one.

    **What is charged is the serialized chunk, not its content**, and the first version of this
    function got that wrong in the same way the count cap it replaces was wrong. `content` is only
    part of what reaches the model: `source_note_id`, `retriever`, `score`, `conflicts_with`,
    `conflicts_total`, `created_by`, `source` and `confidence` all ride beside it, inside JSON
    scaffolding. Measured on one realistic chunk carrying conflicts and provenance — **300
    characters of content against 569 serialized, a 47% under-count**, so a 60,000-character budget
    was really spending about 114,000. Fixing a cap's currency and then measuring the wrong quantity
    is the same error one level down.

    **The serialized size is the right currency on the report path too**, even though a report
    bullet is Markdown rather than JSON: what is being bounded is how much retrieved text one
    section may carry, and the fields the JSON counts (`conflicts_with`, `source`, the citation)
    are exactly the ones `report_note` renders beside the excerpt.
    """
    budget = settings.gather_evidence_max_chars
    kept: list[EvidenceChunk] = []
    spent = 0
    for chunk in chunks[: settings.gather_evidence_max_chunks]:
        # One extra serialization for at most `gather_evidence_max_chunks` chunks, which buys the
        # only number that means anything here: what this chunk actually costs the reader.
        cost = len(chunk.model_dump_json())
        if kept and spent + cost > budget:
            return kept, "chars"
        kept.append(chunk)
        spent += cost
    if len(chunks) > len(kept):
        return kept, "count"
    return kept, None
