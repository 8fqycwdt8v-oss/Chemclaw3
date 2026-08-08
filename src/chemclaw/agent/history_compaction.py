"""Translating a MAF compaction result into row deletions (REV-4, D-151).

MAF's compaction strategies and a SQL table disagree about what compaction *is*, and this module is
the whole of the disagreement.

A strategy **annotates and inserts**: it sets `_excluded` on messages it wants out of the model's
context, and `ToolResultCompactionStrategy` additionally *inserts* a new summary message into the
list, back-linked to the messages it replaced. Nothing is removed — `CompactionProvider` keeps
everything so the annotations survive to the next pass, which is right for an in-memory thread.

Storage **deletes and rewrites**. `session_messages` has one row per message ordered by a
`BIGSERIAL`, and it cannot express "exclude this" or "insert between rows 113 and 115".

So the translation has three jobs, and each is a place the naive version goes wrong:

1. **Track rows by object identity, not position.** The strategy inserts, so indices shift. This is
   the same lesson `chemclaw.agent.session_store.get_messages` already learned about its repair.
2. **Anchor an inserted summary onto a row.** A summary always replaces a group that is being
   deleted anyway, so it can take over that group's *lowest* row id — which is exactly where the
   group sat in `ORDER BY id`, so conversation order survives with no schema change and no ordering
   column. Verified against the real strategy: a collapsed tool group resolves to its call row and
   its result row, and the summary anchors on the call row.
3. **Strip annotations before anything is written back.** `_group`/`_excluded`/`_exclude_reason`
   round-trip through JSONB, and `annotate_message_groups` *trusts* an already-annotated prefix — so
   persisting them would make the next pass group against stale spans. It would also break
   `008_sessions.sql`'s promise that the column holds the MAF payload verbatim.

Pure but for awaiting the strategy: no database, no settings read. That is what lets the
group-integrity tests run everywhere instead of skipping offline, which matters because those tests
are the safety argument.
"""

import copy
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

# The five compaction names below came from `agent_framework._compaction` until 2026-08-08.
# Measured against 1.11.0: every one is exported at the package top level and is the identical
# object, so the private module path was reach the public one already gave.
from agent_framework import (
    EXCLUDE_REASON_KEY,
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    SUMMARY_OF_MESSAGE_IDS_KEY,
    CompactionStrategy,
    Message,
    annotate_message_groups,
    included_messages,
)

from chemclaw.agent.message_pairing import droppable_rows

# The annotation keys a strategy writes onto a message. Imported from MAF's compaction module rather
# than restated as string literals: they are the contract between the strategy and this translation,
# and a renamed key must break the build here rather than silently persist under its old name.
_ANNOTATION_KEYS = (GROUP_ANNOTATION_KEY, EXCLUDED_KEY, EXCLUDE_REASON_KEY)


@dataclass(frozen=True)
class CompactionPlan:
    """What one compaction pass wants done to a session's rows.

    Two operations, never three: rows go away, or a row's payload is replaced by the summary of the
    group it led. Nothing is inserted, which is what keeps this free of a schema change.
    """

    deletes: frozenset[int]
    rewrites: tuple[tuple[int, Message], ...]

    def is_empty(self) -> bool:
        """Whether this plan would touch nothing, so a caller can skip the write entirely."""
        return not self.deletes and not self.rewrites


def _without_annotations(message: Message) -> Message:
    """A copy of `message` with the strategy's bookkeeping removed, safe to persist.

    Shallow-copied and given a fresh properties dict rather than mutated in place: the message is
    still in the list the strategy annotated, and clearing its keys there would corrupt the very
    projection this function is being called in the middle of.
    """
    stripped = copy.copy(message)
    properties = dict(message.additional_properties or {})
    for key in _ANNOTATION_KEYS:
        properties.pop(key, None)
    stripped.additional_properties = properties
    return stripped


async def plan_compaction(
    rows: Sequence[tuple[int, Message]],
    *,
    strategy: CompactionStrategy,
    protected: AbstractSet[int],
) -> CompactionPlan:
    """Decide which rows a compaction pass may delete, and which one carries a group's summary.

    Args:
        rows: The session's rows as `(row_id, message)`, in `id` order. The whole session — the
            pairing closure needs every row to decide whether a group is intact.
        strategy: The compaction policy, which must be the *same* one the context path uses
            (`chemclaw.agent.chemclaw_agent.compaction_strategy`). Durable deletion is strictly more
            destructive than context exclusion, so it must never be more aggressive.
        protected: Rows this pass must not delete whatever the strategy says. The turn that has
            just been written lives here: the composed strategy's fallback can exclude *everything*
            when one payload is oversized, and a turn that deleted the messages it had just stored
            would lose the conversation it was recording.

    Returns:
        The plan. Empty when the strategy excluded nothing that may safely go.
    """
    messages = [message for _, message in rows]
    row_of_object = {id(message): row_id for row_id, message in rows}

    annotate_message_groups(messages)
    await strategy(messages)

    # Built *after* the strategy runs, because it is what assigns `message_id` to messages that had
    # none. The minimum wins on a duplicate id, so an anchor is always the earliest row involved.
    row_of_message_id: dict[str, int] = {}
    for row_id, message in rows:
        if message.message_id:
            existing = row_of_message_id.get(message.message_id)
            row_of_message_id[message.message_id] = min(row_id, existing or row_id)

    kept = included_messages(messages)
    kept_rows = {row_of_object[id(m)] for m in kept if id(m) in row_of_object}
    candidates = {row_id for row_id, _ in rows} - kept_rows - set(protected)
    deletes = droppable_rows(rows, candidates)

    rewrites: list[tuple[int, Message]] = []
    for message in kept:
        if id(message) in row_of_object:
            continue  # an original row, kept as it is
        # An inserted summary. It earns a row only if the group it summarises is actually going;
        # otherwise the originals survive and a summary of them would duplicate history.
        annotation = (message.additional_properties or {}).get(GROUP_ANNOTATION_KEY) or {}
        summarised = [
            row_of_message_id[message_id]
            for message_id in annotation.get(SUMMARY_OF_MESSAGE_IDS_KEY) or []
            if message_id in row_of_message_id
        ]
        if not summarised or not set(summarised) <= deletes:
            continue
        anchor = min(summarised)
        deletes = deletes - {anchor}
        rewrites.append((anchor, _without_annotations(message)))

    return CompactionPlan(deletes=frozenset(deletes), rewrites=tuple(rewrites))
