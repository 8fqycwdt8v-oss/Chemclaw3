"""The agent's way to read many whole protocols at once.

`similar_reactions` answers "have we run a transformation like this?" with ids and Tanimoto scores;
`gather_evidence` returns 240-character excerpts. Neither is a protocol. To read one the agent calls
`expand_note`, which returns the whole body — and that is right for one protocol and wrong for
twenty: twenty are twenty model round-trips against the loop cap, and past
`agent_context_token_budget` the compaction policy reclaims the earliest by replacing them with a
placeholder that takes their citations with them.

`condense_protocols` is the call for the many case. It reads each protocol **once and whole** —
the unit is never a fraction of a procedure — and returns one comparison instead of twenty bodies.
The judgment about what the comparison *means* stays in the skills, as it does for every other tool
here; this only condenses.

Resolution lives here rather than in `agent.condense` because it is the part that knows what a
citation looks like in this system: a knowledge-graph note id, or the `source:doc_id` a mounted
share cites. The condenser itself takes whole protocols and knows nothing about where they came
from, which is what lets a future source reach it without touching it.
"""

import asyncio
import logging
from typing import Any

from chemclaw.agent.condense import Condensation, Protocol
from chemclaw.agent.condense import condense_protocols as _condense
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.tool_registry import tool
from chemclaw.ingest.eln.records import RECORD_TYPE, default_record_store
from chemclaw.ingest.sources.registry import active_retrieve_sources
from chemclaw.kg.graph import build_graph, note_in
from chemclaw.kg.note import Note, external_record_id, resolves_outside_graph

logger = logging.getLogger(__name__)


def _procedure(note: Note) -> str:
    """The recipe out of a note body, or the whole body when it has no procedure section.

    `record_from_ord_reaction` renders the recipe under `## Procedure`, so for an ELN-ingested
    reaction that heading is exactly the prose worth reading — the conditions and outcomes above it
    are already structured in `conditions` and re-reading them with a model would be a second,
    weaker answer to a question the frontmatter has answered.

    A note without the heading — a playbook, a human-written reaction, a campaign — falls back to
    the whole body, because there the prose *is* the content and there is nothing else to read.
    """
    _, _, procedure = note.body.partition("## Procedure")
    return procedure.strip() if procedure.strip() else note.body.strip()


async def _from_record(ref: str) -> Protocol | None:
    """Resolve a `reaction-<id>` citation to the transcription behind it (D-2026-08-25).

    ELN runs left the graph's id space when they became rows, so the lookup above finds none of
    them — and they are the largest class of protocol this tool exists to compare. Without this a
    reaction reference reads as `missing`, which is the same silent hole `_from_share` was written
    to close for share documents, arriving from the other direction.

    The record carries `conditions` for the same reason a note did: the comparison wants numbers,
    not sentences it would have to re-derive from the prose it just rendered.
    """
    if not resolves_outside_graph(ref):
        return None
    record = await default_record_store().read(external_record_id(ref))
    if record is None:
        return None
    return Protocol(
        ref=ref,
        source=record.source,
        title=RECORD_TYPE,
        conditions=record.conditions,
        performed_at=record.performed_at,
        text=record.body,
    )


def _share_readers() -> dict[str, Any]:
    """The enabled sources that can hand back a whole document, by name.

    **Built once per call rather than once per reference**, which is the whole reason this is a
    function returning a map instead of a loop inside `_from_share`. `active_retrieve_sources`
    resolves and constructs every enabled retrieve half, and its own docstring flags that as a
    production concern on this path; measured before the hoist, twelve references rebuilt the
    registry twelve times, and this tool accepts up to `protocol_digest_max_protocols` of them.

    A source with no `read_document` is not a share and simply does not appear.
    """
    readers: dict[str, Any] = {}
    for retriever in active_retrieve_sources():
        reader = getattr(retriever, "read_document", None)
        name = getattr(retriever, "name", "")
        if reader is not None and name:
            readers[name] = reader
    return readers


async def _from_share(ref: str, readers: dict[str, Any]) -> Protocol | None:
    """Resolve a `source:doc_id` citation to the whole document behind it, if any share holds it.

    The address is the one `ShareDocumentRetriever` has always emitted, minus its `#ordinal`. The
    share's own entitlement gate is inside `read_document`, so a caller who may not search a share
    cannot read a document out of it here either.
    """
    source, _, doc_id = ref.partition(":")
    reader = readers.get(source)
    if not doc_id or reader is None:
        return None
    document = await reader(doc_id)
    if document is None:
        return None
    return Protocol(
        ref=ref,
        source=document.path,
        title=document.path,
        text=document.text,
    )


@tool
async def condense_protocols(protocol_refs: list[str]) -> str:
    """Read many whole protocols at once and return one comparison of them.

    Use this instead of calling `expand_note` repeatedly whenever you have more than a handful of
    protocols — the hits from `similar_reactions`, a set of reaction notes, documents cited by
    `gather_evidence`. Each protocol is read **whole and exactly once**: they are never split, and
    one that is too large to read is named rather than silently shortened.

    What comes back is the comparison a process chemist reads: one row per protocol, the recorded
    conditions and outcomes side by side, what each protocol changed relative to the one before it,
    and — read from the procedure text — the solvent, reagents, work-up and observations. Rows are
    in the order the runs were performed where the record has dates, and the table says so when it
    does not, because an undated listing is not a trajectory.

    Every row carries the reference it came from, so cite from the comparison directly and use
    `expand_note` on a single protocol when you need its full text.

    Args:
        protocol_refs: The protocols to condense — knowledge-graph note ids as `similar_reactions`
            and `gather_evidence` return them, and/or `source:doc_id` document citations from a
            mounted share.

    Returns:
        The comparison, followed by what was **not** read and how many protocols it covers. That
        count is every reference you passed — it never means you have seen every protocol on file;
        ask the search that produced these references whether *it* was truncated.

    Raises:
        ChemclawError: When more protocols are asked for than one turn may condense, or than one
            turn's text budget allows. Narrow the set and ask again, or use the campaign synthesis
            for a corpus-scale comparison — a partial answer that did not say so would be worse.
    """
    refs = list(dict.fromkeys(r.strip() for r in protocol_refs if r.strip()))
    if not refs:
        return Condensation(table="", complete=True).render()
    if len(refs) > settings.protocol_digest_max_protocols:
        raise ChemclawError(
            f"{len(refs)} protocols is more than the {settings.protocol_digest_max_protocols} "
            "one call may condense. Narrow the set — by project, by date, or by taking the "
            "highest-similarity hits — and ask again."
        )

    graph = await asyncio.to_thread(build_graph, settings.knowledge_path)
    readers = _share_readers()
    protocols: list[Protocol] = []
    missing: list[str] = []
    for ref in refs:
        note = note_in(graph, ref)
        if isinstance(note, Note):
            protocols.append(
                Protocol(
                    ref=ref,
                    source=note.source or "",
                    title=note.type,
                    conditions=note.conditions,
                    # The date the run was performed, which is what makes the comparison a
                    # timeline rather than a listing (D-162 puts it on `valid_from`).
                    performed_at=note.valid_from,
                    text=_procedure(note),
                )
            )
            continue
        resolved = await _from_record(ref) or await _from_share(ref, readers)
        if resolved is not None:
            protocols.append(resolved)
        else:
            missing.append(ref)
    if missing and not protocols:
        raise ChemclawError(
            f"none of these references resolved to a protocol: {', '.join(sorted(missing))}. "
            "A note id that resolves to nothing is often a citation to a note whose PR-gate "
            "submission has not been merged yet."
        )

    # The text budget, in the currency the count above cannot express: a count of protocols cannot
    # bound their size, which is the `agent_keep_last_conversation_groups` lesson.
    total = sum(len(p.text) for p in protocols)
    if total > settings.protocol_digest_total_max_chars:
        raise ChemclawError(
            f"these {len(protocols)} protocols hold {total} characters, over the "
            f"{settings.protocol_digest_total_max_chars} one call may condense. Narrow the set "
            "and ask again."
        )

    result = await _condense(protocols)
    if missing:
        # Said out loud rather than dropped: a comparison silently missing a protocol the caller
        # asked for reads as a complete answer about a smaller set.
        #
        # **On `unresolved` rather than appended to `degraded`.** These refs have no row: they were
        # never resolved, so nothing about them is in the table. `degraded` means the opposite —
        # the protocol is a row and only its prose is missing — and merging the two made the
        # rendered payload tell the model that a reference nobody could resolve had "recorded
        # figures above", and that a two-row comparison covered all three references it was given.
        result = result.model_copy(update={"complete": False, "unresolved": missing})
    # **Rendered here, not handed over as a model.** A pydantic return is stringified by
    # `langchain_core.tools.base._stringify`, which falls back to `str()` — pydantic's repr — for
    # anything `json.dumps` cannot take. Returning the string means the payload measured and the
    # payload sent are the same thing rather than one chosen by a library's fallback path.
    return result.render()
