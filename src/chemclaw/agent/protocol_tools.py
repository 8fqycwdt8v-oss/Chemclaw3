"""The agent's way to read many whole protocols at once.

`expand_note` is right for one protocol and wrong for twenty: twenty round-trips count against the
loop cap and compaction would reclaim the earliest bodies with their citations. `condense_protocols`
reads each protocol once and whole and returns one comparison; judging what it means stays in the
skills. Resolution of citations (note ids, `reaction-<id>` records, `source:doc_id` share documents)
lives here so the condenser knows nothing about where protocols come from.
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
from chemclaw.kg.note import Note, external_record_ref, resolves_outside_graph

logger = logging.getLogger(__name__)


def _procedure(note: Note) -> str:
    """The recipe out of a note body, or the whole body when it has no procedure section.

    For an ELN reaction the `## Procedure` section is the prose worth reading; its conditions are
    already structured. Without the heading the prose is the content.
    """
    _, _, procedure = note.body.partition("## Procedure")
    return procedure.strip() if procedure.strip() else note.body.strip()


async def _from_record(ref: str) -> Protocol | None:
    """Resolve a `reaction-<id>` citation to the ELN transcription behind it.

    ELN runs are rows, not graph notes, so the note lookup does not find them. The record carries
    `conditions` so the comparison gets numbers rather than prose.
    """
    if not resolves_outside_graph(ref):
        return None
    source, record_id = external_record_ref(ref)
    record = await default_record_store().read(record_id, source)
    if record is None:
        return None
    return Protocol(
        ref=ref,
        source=record.source,
        title=RECORD_TYPE,
        conditions=record.conditions,
        performed_at=record.performed_at,
        species=record.species,
        text=record.body,
    )


def _share_readers() -> dict[str, Any]:
    """The enabled sources that can hand back a whole document, by name.

    Built once per call rather than per reference, because constructing the retrieve sources is
    costly. A source with no `read_document` is not a share and does not appear.
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

    The share's entitlement gate is inside `read_document`.
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
        ChemclawError: In three cases, every one of them about the set you asked for: more
            protocols than one turn may condense, more text than one turn's budget allows, and not
            one reference that resolved to a protocol at all. Narrow the set and ask again, or use
            the campaign synthesis for a corpus-scale comparison — a partial answer that did not
            say so would be worse. **A condensing model that is unreachable, or that cannot be
            built at all, is not one of them and never raises here**: the comparison comes back
            with every recorded figure intact, the columns read out of the prose empty, and the
            payload saying in words which protocols went unread. This paragraph said "or than one
            turn's text budget allows" and stopped, over a third `ChemclawError` and a period in
            which an unbuildable client did escape it.
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
                    # The date the run was performed (`valid_from`), which makes the comparison a
                    # timeline.
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
            "A note id that resolves to nothing is a citation to a note that does not exist — "
            "check the id rather than assuming it is pending."
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
        # Said out loud rather than dropped, so a partial comparison does not read as complete. On
        # `unresolved`, not `degraded`: these refs have no row at all, while `degraded` means a row
        # whose prose is missing.
        result = result.model_copy(update={"complete": False, "unresolved": missing})
    # Rendered here so the payload sent is exactly this string, not a library's `str()` fallback of
    # a pydantic model.
    return result.render()
