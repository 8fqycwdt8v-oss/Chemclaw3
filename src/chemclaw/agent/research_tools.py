"""The agent's cross-source evidence gatherer (plan Phase 5b, generalized).

`gather_evidence` is the one tool that sweeps **every** internal source behind the report
harness's `SourceRetriever` contract and returns cited evidence in a single call — the
substrate for open-ended research questions ("what has been tried / what were the levers /
what matters when a certain group is present"). It is deliberately source-agnostic: today it
unions the knowledge graph (every note type — reactions, campaigns, optimization campaigns,
playbooks, reports) with reaction-fingerprint search; adding a source later (analytics,
external literature) is one more retriever in `_text_retrievers`, not a change here or to the
agent. Every returned chunk carries the id of the note it came from, so the agent can cite it
and `expand_note` for the full recipe/conditions/outcomes.

The judgment — decomposing the question, deciding which anchor to search on, separating
evidenced fact from transferred analogy, and drafting new protocols — lives in the
`deep-research` skill, not here. This tool only gathers.
"""

from datetime import date
from itertools import zip_longest
from typing import Any

from chemclaw.agent.framing import defang, frame_untrusted
from chemclaw.core.config import settings
from chemclaw.core.tool_registry import tool
from chemclaw.ingest.sources.registry import active_retrieve_sources
from chemclaw.retrieval.evidence import EvidenceChunk, SourceRetriever
from chemclaw.retrieval.fanout import sweep_sources
from chemclaw.retrieval.hybrid import reciprocal_rank_fusion
from chemclaw.retrieval.retrievers import FingerprintReactionRetriever
from chemclaw.science.fingerprints.store import default_reaction_store

# Test seam: swap the production reaction store for an in-memory one without a database.
_reaction_store = default_reaction_store


def _text_retrievers() -> list[SourceRetriever]:
    """The active retrieve halves from the data-source registry (plan F7).

    Adding a text source is a registry entry + a config token now, not an edit here — the default
    (`graph`) yields exactly the single `GraphRetriever` this returned before, so behavior is
    unchanged until a deployment activates another source.
    """
    return list(active_retrieve_sources())


def _sources(reaction_smiles: str | None) -> list[tuple[str, SourceRetriever]]:
    """Every source this sweep asks, named, in the order the merge downstream expects.

    The name is the *retriever's own* name rather than one invented here, because it labels the
    per-source counter and the per-branch stream event, and both are read against the `retriever`
    field on the chunks that come back. Two names for one source would make a starved leg look like
    a missing one.

    The fingerprint retriever is last and conditional: it answers a structural anchor rather than
    the text query, so it exists only when a `reaction_smiles` was given. Appending it keeps the
    text sources' relative order stable whether or not an anchor was passed.
    """
    sources: list[tuple[str, SourceRetriever]] = [
        (retriever.name, retriever) for retriever in _text_retrievers()
    ]
    if reaction_smiles is not None:
        # The anchor, not the query: this source searches structures. `sweep_sources` asks every
        # branch the same question, so the anchor rides in as this source's own query via a
        # retriever bound to it.
        anchored = _AnchoredRetriever(_reaction_store(), reaction_smiles)
        sources.append((anchored.name, anchored))
    return sources


class _AnchoredRetriever:
    """A fingerprint retriever that answers the structural anchor rather than the text query.

    The fan-out asks every source one question, which is right for text sources and wrong for this
    one — a `reactants>>products` anchor is not the chemist's sentence. Binding the anchor here
    keeps the fan-out uniform instead of teaching it that one source is special, and keeps the
    substitution to the one place that knows why the two questions differ.
    """

    # The *inner* retriever's name, not one chosen here. The chunks this returns are built by
    # `FingerprintReactionRetriever` and carry `retriever="reaction-fingerprint"`, and that same
    # string is the key `settings.retrieval_source_weights` is looked up by in the fusion. A label
    # invented here would have made the branch's counter and stream event name a source that
    # appears nowhere in the evidence — a starved leg looking like a missing one, which is the
    # exact confusion this phase exists to remove.
    name = FingerprintReactionRetriever.name

    def __init__(self, store: Any, reaction_smiles: str) -> None:
        """Bind the store and the anchor this retriever will answer with."""
        self._inner = FingerprintReactionRetriever(store)
        self._anchor = reaction_smiles

    async def retrieve(self, _query: str, _filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Search structures for the bound anchor, ignoring the sweep's text query and filters.

        Filters are dropped rather than forwarded because the fingerprint store holds structures
        and not dates — which `gather_evidence`'s own docstring already warns a caller about ("hits
        from a `reaction_smiles` anchor come back unwindowed").
        """
        return await self._inner.retrieve(self._anchor, {})


def _interleave_dedup(ranked_lists: list[list[EvidenceChunk]]) -> list[EvidenceChunk]:
    """Round-robin the per-source hit-lists into one, dropping exact (note, content) repeats.

    The `graph` retrieval mode's cross-source merge, and the thing that makes the cap at the end of
    `gather_evidence` fair. **A source's rank position is comparable across sources; its score is
    not** — `EvidenceChunk.score` is a note's `confidence` from the graph, a `ts_rank` from
    Postgres FTS, a cosine from the dense index and a Tanimoto from the fingerprint store, and the
    chunk's own docstring says so. Concatenating the lists and then sorting the union by that
    number let one source's scale decide the whole sweep, and the cap then kept a prefix of
    whichever scale ran highest.

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


def _as_date(value: str, field: str) -> date:
    """Parse an ISO date argument, or fail with a message the model can act on.

    A tool argument comes from the model, so a malformed one is a prompt-level mistake, not a
    bug: naming the field and the expected format is what lets the next attempt be correct,
    where a bare `ValueError` from the stdlib would not.
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
) -> list[EvidenceChunk]:
    """Gather cited evidence for a research question from every internal source at once.

    Runs each text source (the knowledge graph, and any future literature/analytics source)
    on `query`, and — when an anchor reaction is given — also pulls structurally similar past
    reactions (DRFP). Results are merged and de-duplicated. Empty is a valid answer (nothing
    on file), never invented.

    Args:
        query: The natural-language question or key terms (matched over note id/tags/body).
        reaction_smiles: Optional `reactants>>products` anchor to also pull similar reactions.
        note_type: Optional graph filter, e.g. "reaction", "optimization-campaign", "playbook".
        tag: Optional graph tag filter (e.g. a project name).
        since: Optional ISO date (YYYY-MM-DD); keep only notes dated on or after it — for a
            reaction note, the day the experiment was run. Use it for "what have we tried
            recently"; note that a note with no date is excluded, not assumed to be in range.
            It windows the **note** sources only: the fingerprint store holds structures, not
            dates, so hits from a `reaction_smiles` anchor come back unwindowed and a call that
            uses both returns a mix. Filter on the anchor or on the window, not both, when the
            answer has to be "only what happened in this period".
        until: Optional ISO date (YYYY-MM-DD); keep only notes dated on or before it.

    Returns:
        Evidence chunks, each with its content, the `source_note_id` to cite/expand, and which
        retriever found it. Capped at the configured budget so a broad sweep does not flood the
        context; if you hit the cap, narrow the query (a `note_type`/`tag`/date filter) rather
        than assume you have seen everything.
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

    # One ordered hit-list per source; each retriever ranks its own hits (best first).
    #
    # Swept as a `Send` fan-out — one branch per source, fanning into one `operator.add` field
    # (`chemclaw.retrieval.fanout`, M10). This was already concurrent before, and the concurrency
    # is *not* what changed: `asyncio.gather` cost the maximum of the sources' latencies rather
    # than the sum, and still would. What a branch adds is that it reports what it contributed —
    # so a source returning nothing is distinguishable from a source nobody asked, which is
    # precisely what `D-2026-08-01-a-cap-that-starves-a-source` needed and did not have.
    #
    # Order is preserved by the fan-in, deliberately: both merge modes below read the lists in
    # source order (RRF takes a note's representative chunk from the first list that found it, and
    # the round-robin interleaves in list order), so completion order would make one sweep's
    # evidence differ from the next for no visible reason.
    ranked_lists = await sweep_sources(_sources(reaction_smiles), query, filters)

    # `hybrid` fuses the per-source rankings (a note any source ranks highly rises); `graph` (the
    # default) round-robins them. Both are cross-source-fair under the cap below, differing in
    # whether a note found twice is *rewarded* for it. Either way graph expansion stays the
    # reasoning path.
    if settings.retrieval_mode == "hybrid":
        # RRF already produces the cross-source ranking (best first), so it *is* the order the cap
        # keeps — re-sorting by a single source's raw score would discard the fusion.
        ranked = reciprocal_rank_fusion(
            ranked_lists,
            k=settings.retrieval_fusion_k,
            weights=settings.retrieval_source_weights_map,
        )
    else:
        # Round-robin, not a flat union re-sorted by score: the cap below has to be survivable by
        # every source, and each retriever has already ranked its own hits by the only signal that
        # is meaningful within it (KM-5). Sorting the union by `score` compared a note's confidence
        # against a `ts_rank` against a cosine, which is the comparison `EvidenceChunk.score`
        # documents as invalid — see `_interleave_dedup` for what it measured.
        ranked = _interleave_dedup(ranked_lists)
    # Frame each chunk's content as retrieved data before it enters the model context, so a
    # note body carrying adversarial text is read as evidence to cite, not an instruction.
    #
    # `source` is neutralized on the same pass and for the same reason, having been missed on the
    # first: it is a *second* retrieved-text channel on the very same object. The warehouse
    # retriever builds it as `<source>:<relation>:<row key>`, so a warehouse row's own key reaches
    # the prompt through it — outside the envelope, where a forged delimiter would be read as the
    # envelope closing. `defang` rather than `frame_untrusted`, because a label is not evidence and
    # wrapping it would make the citation unreadable.
    return [
        chunk.model_copy(
            update={
                "content": frame_untrusted(chunk.content, note_id=chunk.source_note_id),
                "source": defang(chunk.source),
                # `source_note_id` for the same reason and from the same producer: the warehouse
                # retriever builds both from one row key, one statement apart. `safe_id` sanitizes
                # only the *copy* interpolated into the envelope's `id=` attribute — the field on
                # the returned model is what the tool result serializes, and it reached the model
                # raw. Defanged rather than `safe_id`'d, because a citation has to stay resolvable.
                "source_note_id": defang(chunk.source_note_id),
            }
        )
        for chunk in ranked[: settings.gather_evidence_max_chunks]
    ]
