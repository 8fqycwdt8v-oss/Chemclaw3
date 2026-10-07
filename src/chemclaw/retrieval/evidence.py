"""The report harness's source-agnostic contract.

An `EvidenceChunk` must carry `source_note_id`; the harness refuses to synthesize anything not
tied to a note. A `SourceRetriever` (`retrieve(query, filters)`) is the only thing the harness
core knows, so a new source is a new retriever behind this interface, never a core change.
"""

from collections.abc import Iterable
from datetime import date
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class EvidenceChunk(BaseModel):
    """One retrieved fact and its mandatory citation back to the source note."""

    content: str = Field(min_length=1)
    source_note_id: str = Field(min_length=1)
    # How the chunk was found (which retriever) — provenance for the report footer.
    retriever: str = Field(min_length=1)
    # A relevance score in [0, 1], set by each retriever in its own terms (note confidence,
    # similarity, `ts_rank`, cosine): a ranking heuristic within one source, not comparable across
    # sources. Merges go by rank position, and `hybrid.restated_as_position` rewrites this to
    # `1 / (1 + position)` in the merged list so the printed number matches the order; `confidence`
    # carries the note's own confidence. The neutral default keeps a retriever that forgets to set
    # it mid-ranking.
    score: float = Field(default=0.5, ge=0.0, le=1.0)
    # Notes this chunk's source note is known or suspected to disagree with (`kg.conflicts`). A
    # flag, never a filter: retrieval cannot decide which note is right, and returning both silently
    # reads as corroboration. Holds the strongest disagreements, declared first, up to
    # `conflict_max_per_note`.
    conflicts_with: list[str] = Field(
        default_factory=list,
        description=(
            "Ids of notes the corpus records as disagreeing with this chunk's own note. These "
            "notes disagree; do not read this and a conflicting note as two independent "
            "confirmations. The disputing note may not be in this sweep at all."
        ),
    )
    # The total number of disagreements, so a truncated list never reads as complete ("3 of 141").
    conflicts_total: int = Field(
        default=0,
        ge=0,
        description=(
            "How many notes disagree with this one in total — larger than len(conflicts_with) "
            "when only the strongest are listed."
        ),
    )
    # Who authored the source note, where it came from, and how sure it is, so the model can weigh a
    # claim by its claimant. `created_by` is `""` rather than `"human"` when the retriever cannot
    # establish it (a structural hit has no note author): defaulting would assert unchecked
    # provenance.
    created_by: str = ""
    source: str = ""
    confidence: float | None = None
    # When the source note stopped being valid, or `None` while its window is open. A date-windowed
    # sweep deliberately serves retired notes, and this field says so. Not a filter: the caller
    # asked for the period.
    valid_to: date | None = None
    # Which of the query's terms (`kg.search.query_terms`) this chunk's note actually contains.
    #
    # The graph leg widens to any-term matches and always fills `retrieval_top_k`, so an absent
    # answer looks like a present one. Which terms matched (e.g. not the compound name) is what lets
    # the model qualify an answer; a count does not discriminate. A separate field because `score`
    # is overwritten by the merged rank.
    #
    # `None` means the source did not report it (legs built from raw document text never tokenise
    # the query); an empty list on a note-backed chunk is a real statement.
    matched_terms: list[str] | None = Field(
        default=None,
        description=(
            "Which of the query's search terms this note's text contains, or null where the "
            "source does not report term matching. Compare it against the question that was "
            "asked: a chunk that matched only the framing words of a question and none of its "
            "subject was surfaced by a widened search and may not answer it at all."
        ),
    )


class EvidenceSweep(BaseModel):
    """What one `gather_evidence` call found, **and what it could not say**.

    The tool returned a bare `list[EvidenceChunk]` and both of its silences were invisible in that
    shape, which is why the shape changed:

    - **A cut looked like a corpus.** Hitting the cap returned a short list identical to a small
      corpus, and the tool's own docstring tells the model that empty means "nothing on file, never
      invented". `truncated_by` says which bound bit, and `total_before_cap` says how much there
      was — the rule `FingerprintSearch.hits_truncated` and `EvidenceChunk.conflicts_total` already
      follow, applied to the sweep itself.
    - **A partial outage looked like a partial corpus.** `gather_evidence` raises when *every*
      source fails, correctly; when one of four fails it returned real-but-incomplete evidence with
      the degradation visible only on the stream. Its own comment named the fix and deferred it:
      "closing that needs the return type to carry provenance, which is a contract change beyond
      this fix". `sources_failed` is that field.
    """

    chunks: list[EvidenceChunk] = Field(default_factory=list)
    # `None` when everything found was returned; otherwise the bound that bit first: a `count` cut
    # narrows with a filter, a `chars` cut by narrowing sources. `total_before_cap` says how much is
    # still unseen.
    truncated_by: Literal["count", "chars"] | None = None
    # How many chunks survived merging before either cap, so "40 of 300" is expressible.
    total_before_cap: int = Field(default=0, ge=0)
    # Sources that could not be asked at all. Empty is the ordinary case; a name here means this
    # answer is about less than the whole corpus, whatever the chunks say.
    sources_failed: list[str] = Field(default_factory=list)
    # What each source handed to the merge (pre-merge counts), by name, so "found nothing", "not
    # configured" and "declined" are distinguishable. A leg out-competed at the cap still shows its
    # work here; the kept-after-merge half is metered by `fanout.record_kept_chunks`.
    sources: dict[str, int] = Field(default_factory=dict)
    # How many hits a source discarded at its own bound, before the merge saw them, by name. With
    # `retrieval_top_k` small this is usually the cut that bites, and `truncated_by` cannot see it.
    # A source absent here cut nothing or cannot say (dense and lexical legs push `LIMIT k` into the
    # index), so unknown is an absence, never a zero.
    sources_truncated: dict[str, int] = Field(default_factory=dict)
    # Sources that declined the question, by name -> the reason they gave (`RetrieverSkip`).
    # Distinct from `sources_failed`: a failure is an outage, a skip is a fact about the deployment
    # or the call.
    sources_skipped: dict[str, str] = Field(default_factory=dict)


class Hits(list[EvidenceChunk]):
    """What one source returned, and how many it had **before its own bound cut them**.

    A `list` subclass so every existing caller keeps working; only the retrievers that cut set
    `found`, and only `fanout` reads it. Not a retriever attribute (one instance serves concurrent
    turns) and not per-chunk (a source returning nothing would have nowhere to put it).

    `found` is `None` when the source cannot say (index legs that push `LIMIT k` down), which is not
    "did not cut". `+` and slicing return plain lists, correctly: a concatenation has no single
    total.
    """

    __slots__ = ("found",)

    def __init__(self, chunks: Iterable[EvidenceChunk] = (), *, found: int | None = None) -> None:
        """Hold `chunks`, recording that `found` existed before this source's own bound."""
        super().__init__(chunks)
        self.found: int | None = found

    @property
    def dropped(self) -> int:
        """Hits discarded at this source's own bound; 0 when it cut none or cannot say."""
        return 0 if self.found is None else max(0, self.found - len(self))


class RetrieverSkip(Exception):
    """A source declining to answer, with the reason a reader can act on.

    Raised when a retriever cannot meaningfully ask (an unentitled caller, an unservable filter, an
    empty notes tree), as opposed to asking and finding nothing. The fan-out reports it as a skip,
    distinct from a failure or an empty result.
    """

    def __init__(self, reason: str) -> None:
        """Carry `reason` both as the exception message and as a named field."""
        super().__init__(reason)
        self.reason = reason


@runtime_checkable
class SourceRetriever(Protocol):
    """Retrieve evidence for a query from one internal source. One per source."""

    name: str

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return evidence chunks answering `query` under `filters` (may be empty)."""
        ...
