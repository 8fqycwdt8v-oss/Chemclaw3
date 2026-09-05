"""The report harness's source-agnostic contract (plan steps 5b.1, 5b.2).

An `EvidenceChunk` is a retrieved fact that **must** carry a back-reference to the source note
it came from (`source_note_id`) — the harness refuses to synthesize anything not tied to a
note (no fabricated statistics, 5b.4). A `SourceRetriever` is the only thing the harness core
knows: a `retrieve(query, filters)` that returns evidence chunks. Concrete sources (graph,
fingerprint search, analytics) implement it as thin adapters, so adding a source — later even
external literature — is a new retriever behind this interface, never a change to the core (G6).
"""

from collections.abc import Iterable
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class EvidenceChunk(BaseModel):
    """One retrieved fact and its mandatory citation back to the source note."""

    content: str = Field(min_length=1)
    source_note_id: str = Field(min_length=1)
    # How the chunk was found (which retriever) — provenance for the report footer.
    retriever: str = Field(min_length=1)
    # A relevance/support score in [0, 1], higher = keep first when a sweep must truncate (KM-5).
    # Each retriever sets it in its own terms — graph hits by the note's `confidence`, structural
    # hits by similarity, index hits by `ts_rank` or cosine — so it is a ranking heuristic, not a
    # calibrated cross-source probability.
    #
    # **Which is why it orders a source's own list and nothing wider.** `gather_evidence` used to
    # sort the union of every source by this number before capping it, and measurably starved the
    # lexical leg to zero surviving chunks: a note's `confidence` and a Postgres `ts_rank` are not
    # the same quantity and the higher scale simply won. Both merge modes now go by rank position,
    # which *is* comparable across sources, and each retriever applies this score inside its own
    # ranking where it means something.
    #
    # **What reaches the model is therefore not the finder's number.** `hybrid.restated_as_position`
    # rewrites this field to `1 / (1 + position)` in the merged list, in both modes, because a
    # number that contradicts the order it is printed beside is worse than no number. The finder's
    # own value governs its source's ranking and the cap, and is spent by the time the chunk is
    # returned; `confidence` below carries the note's confidence as a field of its own.
    #
    # Defaults to a neutral 0.5: every current retriever sets it explicitly, so this only governs a
    # future retriever that forgets to — and neutral keeps such a chunk in the middle of its
    # source's ranking rather than silently pinning it last (and truncated).
    score: float = Field(default=0.5, ge=0.0, le=1.0)
    # Notes this chunk's source note is known or suspected to disagree with (`kg.conflicts`,
    # KM-8). A *flag*, never a filter: retrieval has no basis for deciding which of two curated
    # notes is right, and silently returning both is what made two contradictory notes read as
    # corroboration. Empty for the ordinary case, so a reader sees the marker only when there is
    # something to see.
    #
    # The *strongest* disagreements, declared ones first, not all of them: on a corpus shaped like
    # a real programme this list ran to ~141 ids per chunk, which is a fact about the corpus rather
    # than a signal about the note (`conflict_max_per_note`).
    conflicts_with: list[str] = Field(default_factory=list)
    # How many disagreements there are in total, which is not always `len(conflicts_with)`. Carried
    # because a truncated list with nothing saying so reads as a complete one — the same rule the
    # tool-result number cap follows. Renderers say "3 of 141" when the two differ.
    conflicts_total: int = Field(default=0, ge=0)
    # Who authored the source note, where it came from, and how sure it is (D-160). `NoteRef` has
    # exposed all three to `find_notes`/`expand_note` since KM-6; the sweep that gathers most of
    # the evidence an answer is built on carried none of them, so the model saw a claim and no way
    # to weigh who was claiming it. `confidence` did reach here — as `score`, a truncation-order
    # signal — which is not the same thing as being *told* a note is uncertain.
    #
    # This is harmless while everything readable was human-merged, and becomes a correctness bug
    # the moment a second, ungated tier exists (D-161). It ships first and on its own for that
    # reason.
    #
    # `created_by` is deliberately `""`, not `"human"`, when the retriever could not establish it:
    # a structural hit is generated from the fingerprint index and has no note author. Defaulting
    # to "human" would assert provenance nobody checked, in the one field whose whole purpose is
    # to be trusted.
    created_by: str = ""
    source: str = ""
    confidence: float | None = None


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
    # `None` when everything found was returned. Otherwise which bound cut the list — the two are
    # separately actionable: a `count` cut narrows with a filter, a `chars` cut narrows the sources.
    #
    # **The bound that bit first**, when both would have. That is the actionable one, but it means a
    # reader who narrows on `chars` may then meet the count cap; `total_before_cap` is what says how
    # much is still unseen either way.
    truncated_by: Literal["count", "chars"] | None = None
    # How many chunks survived merging before either cap, so "40 of 300" is expressible.
    total_before_cap: int = Field(default=0, ge=0)
    # Sources that could not be asked at all. Empty is the ordinary case; a name here means this
    # answer is about less than the whole corpus, whatever the chunks say.
    sources_failed: list[str] = Field(default_factory=list)
    # What each source contributed to the merge, by name — the per-branch fact the fan-out
    # already computed and then dropped at this boundary, so the model could not tell "the share
    # leg found nothing", "the share leg isn't configured" and "the share leg declined" apart:
    # three different answers rendered identically as an absence. Counts are pre-merge (what the
    # source handed the sweep), so a source out-competed at the cap still shows its work.
    #
    # **Pre-merge is right for this field and wrong as the only measurement**, which is why
    # `fanout.record_kept_chunks` exists: a leg that hands over thirty chunks and survives the
    # merge with none reads as healthy here, and that is exactly the shape
    # `D-2026-08-01-a-cap-that-starves-a-source` measured. The metric pair
    # (`chemclaw_evidence_source_chunks_total` and `chemclaw_evidence_source_kept_total`) carries
    # both halves, so the ratio is alertable across turns while this field stays what a single
    # answer's reader needs.
    sources: dict[str, int] = Field(default_factory=dict)
    # How many hits a source discarded at **its own** bound, before the merge ever saw them —
    # by name, and only for the sources that both cut and can say so.
    #
    # **`sources` and `truncated_by` together were still not the whole truth.** Both describe the
    # *merge*: `sources` counts what a leg handed over, `truncated_by` names which of
    # `gather_evidence_max_chunks`/`_max_chars` cut the merged list. Neither can see a leg that
    # truncated before handing anything over — and `retrieval_top_k` is 8, so on the shipped
    # configuration that is the *only* cut that ever bites. Measured on 5,000 matching notes: the
    # sweep reported `chunks=8, total_before_cap=8, truncated_by=None` while 4,992 were dropped
    # inside the graph leg. "A cut does not look like a corpus" was true of the merge and false of
    # the legs.
    #
    # A source absent from this dict either cut nothing or **cannot say** — the dense and lexical
    # legs push `LIMIT k` into the index and do not know what they did not fetch. That is why the
    # unknown is an absence rather than a zero: a zero here would assert "nothing was cut" on the
    # one leg that cannot check it.
    sources_truncated: dict[str, int] = Field(default_factory=dict)
    # Sources that declined the question, by name -> the reason they gave (`RetrieverSkip`).
    # Distinct from `sources_failed` because the fixes differ: a failure is an outage, a skip is
    # a fact about the deployment or the call (an unentitled actor, a filter a source cannot
    # serve, a notes directory with nothing in it).
    sources_skipped: dict[str, str] = Field(default_factory=dict)


class Hits(list[EvidenceChunk]):
    """What one source returned, and how many it had **before its own bound cut them**.

    A `list` subclass rather than a wrapper, and that is the whole design decision. The count has to
    travel from the retriever that cuts to the fan-out that reports, and the three shapes that could
    carry it were each worse:

    - **Changing `retrieve`'s return type.** Honest, and it churns 84 call sites of which **81 are
      tests** — every `len(await r.retrieve(...))` and `assert ... == []` in the suite — for no
      behavioural gain in any of them. A diff whose signal-to-noise is 3:81 is one nobody reviews.
    - **A mutable attribute on the retriever.** One instance serves concurrent turns, so the count
      would be whichever turn wrote last. That is the defect, not a way to report it.
    - **The count repeated on every chunk.** N copies of one fact, and a source that returns zero
      chunks then has nowhere to put it — which is exactly the case worth reporting.

    A `Hits` *is* a list, so every existing caller and every test keeps working unchanged; only the
    retrievers that cut set `found`, and only `fanout` reads it.

    **`found` is `None` when the source cannot say, and that is not the same as "did not cut".**
    `GraphRetriever` materialises its whole candidate set and then truncates, so it knows both
    numbers. The dense and lexical legs push `LIMIT k` into the index and genuinely do not know what
    they did not fetch — reporting `found == len(self)` for them would assert "nothing was cut" on
    the one leg that cannot check, which is the ambiguous zero
    `D-2026-08-03-a-metric-must-declare-what-it-can-see` is about. `None` says "unknown"; a reader
    that wants a total must treat it as unknown rather than as agreement.

    `+` and slicing return plain lists, losing `found`. That is correct rather than unfortunate: a
    concatenation of two sources' hits has no single pre-cut total to carry.
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

    Raised by a retriever when it *cannot meaningfully ask* — an unentitled caller, a filter the
    source cannot serve, a notes tree with nothing in it — as opposed to asking and finding
    nothing. The fan-out reports it as a skip rather than a failure or a zero: all three used to
    collapse into an indistinguishable `[]`, which is the D-2026-08-01 class one category over.
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
