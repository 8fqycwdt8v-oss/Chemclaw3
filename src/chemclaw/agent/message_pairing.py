"""The one rule a stored conversation must satisfy: every tool call carries its result.

Anthropic (and every other tool-calling API) rejects a thread in which a `tool_use` block is not
answered by a matching `tool_result` — "tool_use ids were found without tool_result blocks". A
thread that acquires an unmatched call is not one bad turn; it is a poison pill replayed on every
subsequent turn, so whatever holds it is bricked until somebody edits it.

**Nothing here heals such a thread any more, and that is deliberate.** This module used to carry a
read-time repair, because MAF's durable history *was* the thread sent to the model and a pod
eviction between the two writes left an orphan in it. The graph builds its thread from the
checkpointer now (D-2026-08-10 §2), so what is left are the two jobs that were never about healing:

- **A guard on deletion.** `droppable_rows` is the rule every code path that deletes conversation
  rows goes through, so an age cutoff cannot take one half of a pair. It is used by
  `durable/retention.py`, and it contracts rather than expands, so the worst case is a straddling
  group surviving one more sweep.
- **Assertions.** `unmatched_call_ids`, `unmatched_result_ids` and
  `calls_without_adjacent_results` are what a test uses to prove code that deletes or assembles
  messages did not strand anything. None has a production caller, and none should acquire one:
  their value is that they refuse to fix what they find, so a bug that strands a pairing fails a
  test instead of being cleaned up behind.

  The third was briefly deleted as having no subject — the previous framework assembled and sent
  the wire payload it checked, and the graph builds its thread from the checkpointer instead. That
  reasoning missed the one thing that *does* still assemble a payload: `agent/compaction.py` edits
  the message list a model call is handed, and the on-the-wire rule is precisely what its tests
  must hold it to. A narrowing that stranded a `tool_use` would brick the thread it was narrowing.

**`droppable_rows` takes call ids, not messages, and that is the shape the rule always wanted.**
Pairing is a relation between *identifiers*; the message around them was only ever how the caller
happened to hold them. Reading them out is `stored_call_ids`, the one function that knows the
stored shapes — and it has to know **two**, because `session_messages` holds whichever shape the M6
conversion pass has reached (`agent/message_migration.py`). That is also what removed the last
framework import from the deletion path: a rule deciding what a nightly sweep destroys should not
be able to break because a library renamed a content type.

The assertions still take LangChain messages, because their callers are tests holding messages.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Any

from langchain_core.messages import BaseMessage

logger = logging.getLogger(__name__)

# MAF's content-type discriminators, kept because *stored rows* still carry them: every
# `session_messages` row written before the M6 conversion is a `Message.to_dict()`, and the
# retention sweep reads them whether or not the conversion has run. They are read here and nowhere
# else, which is the point of `stored_call_ids` existing.
#
# These two strings decide which rows a data-destroying nightly sweep may delete, so a mistake in
# them does not fail loudly — it changes what gets destroyed. Measured against a plausible rename:
# `droppable_rows([(1, {"c"}), (2, {"c"})], {1})` goes from `set()` (the partner correctly
# protected) to `{1}` — the sweep deletes the call row and strands its answer, leaving a thread the
# API rejects outright. Silent: no exception, no failed activity, conversations that stop rendering
# days later and no longer say why.
_MAF_CALL = "function_call"
_MAF_RESULT = "function_result"

# The stamp `agent/message_migration` writes for a converted row. Named from that module so the two
# cannot drift: the whole point of taking the stamp is that one rule decides what a row is.
_LANGCHAIN_SHAPE = "langchain"


def stored_call_ids(payload: Mapping[str, Any], shape: str | None = None) -> frozenset[str] | None:
    """The tool-call ids one stored `session_messages.message` row mentions, in either direction.

    **Both stored shapes, because the table holds both**, and **the stamp decides which** — the
    same rule `session_store.message_from_row` goes by. A row written before the M6 conversion is a
    MAF `Message.to_dict()` (`{"role", "contents"}`); one written after is a LangChain
    `message_to_dict()` (`{"type", "data"}`); the conversion pass is resumable, so any given row may
    be either.

    This used to sniff the payload and ignore the stamp, which made **two functions decide one
    question by two rules** — on a table where one of them governs a nightly *deletion*. Two rules
    that agree today are two rules that can stop agreeing, and the direction that matters is the
    destructive one. Reading the stamp first and falling back to the payload keeps the unstamped
    historical rows working (that fallback is why the sniffing existed) without leaving a second
    authority for what a row *is*.

    Returns `None` for a payload matching neither shape, and that is not the same as "no ids".
    Empty means "this row is in no pairing, so it may be disposed of on its own"; `None` means "this
    row cannot be read, so nothing can be concluded about what it is paired with". Collapsing the
    two would make an unreadable row look pairing-free and therefore *droppable*, which is the one
    direction this module exists to prevent.

    Args:
        payload: One row's `message` column, as stored.
        shape: The row's `message_shape` stamp, or `None` for a row written before the stamp
            existed — which is every historical row, and is why the payload fallback stays.

    Returns:
        The call ids the row mentions, whether as a call or as its answer, or `None` when the row
        matches neither stored shape.
    """
    if shape == _LANGCHAIN_SHAPE:
        return _langchain_call_ids(payload)
    if "contents" in payload:
        contents = payload.get("contents")
        if not isinstance(contents, list):
            return None
        return frozenset(
            str(item["call_id"])
            for item in contents
            if isinstance(item, dict)
            and item.get("type") in (_MAF_CALL, _MAF_RESULT)
            and item.get("call_id") is not None
        )
    return _langchain_call_ids(payload)


def _langchain_call_ids(payload: Mapping[str, Any]) -> frozenset[str] | None:
    """The ids a LangChain-shaped row mentions, or `None` when it is not that shape either.

    An assistant message carries its calls in `tool_calls`; a tool message carries the id it answers
    in `tool_call_id`. Both directions, because a component is joined by either — see
    `droppable_rows`.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    ids = {
        str(call["id"])
        for call in data.get("tool_calls") or []
        if isinstance(call, dict) and call.get("id") is not None
    }
    answered = data.get("tool_call_id")
    if answered is not None:
        ids.add(str(answered))
    return frozenset(ids)


def _answered_id(message: BaseMessage) -> str | None:
    """The call id this message answers, or `None` when it answers none.

    `tool_call_id` is `ToolMessage`'s, not `BaseMessage`'s, so it is read rather than accessed —
    these functions take the base type on purpose, because their whole job is to walk a mixed list.
    """
    answered = getattr(message, "tool_call_id", None)
    return str(answered) if answered else None


def calls_without_adjacent_results(messages: Sequence[BaseMessage]) -> set[str]:
    """Return the ids of tool calls whose answer is not in the *immediately following* message.

    The stricter, on-the-wire form of the rule: the API does not merely require that an answer
    exists somewhere, it requires it in the very next block — "tool_use ids were found without
    tool_result blocks **immediately after**". The two checks differ exactly where duplicated
    history does: replaying a call the transcript already answered leaves a second, unanswered copy
    that an exists-somewhere check still considers satisfied, because the id does appear answered
    once.

    Use this to validate what is about to be sent. It is deliberately *not* the rule storage goes
    by — there a merely out-of-order pair is intact history and must not be deleted, which is what
    `droppable_rows` enforces.
    """
    missing: set[str] = set()
    for index, message in enumerate(messages):
        called = {
            str(call["id"])
            for call in getattr(message, "tool_calls", None) or []
            if call.get("id") is not None
        }
        if not called:
            continue
        following = messages[index + 1] if index + 1 < len(messages) else None
        next_id = _answered_id(following) if following is not None else None
        answered = {next_id} if next_id is not None else set[str]()
        missing |= called - answered
    return missing


def unmatched_call_ids(messages: Sequence[BaseMessage]) -> set[str]:
    """Return the ids of tool calls that no tool message answers.

    Order-independent on purpose: an answer is valid wherever it sits in the list, so this reports
    genuinely unanswered calls rather than merely out-of-order ones.

    One of the pair a deletion has to be checked against — see `unmatched_result_ids` for why both
    directions are asked and neither is repaired.
    """
    answered = {i for i in (_answered_id(m) for m in messages) if i is not None}
    return {
        str(call["id"])
        for message in messages
        for call in getattr(message, "tool_calls", None) or []
        if call.get("id") is not None and str(call["id"]) not in answered
    }


def unmatched_result_ids(messages: Sequence[BaseMessage]) -> set[str]:
    """Return the ids of tool *results* that no tool call accounts for.

    The mirror of `unmatched_call_ids`, and both are deliberately assertions rather than repairs
    (D-145). There used to be an asymmetry here: a read-time repair stripped an unanswered *call*,
    so that direction self-healed while a stranded *result* did not. The repair is gone with the
    MAF thread that needed it, and the symmetry is the better state — stripping either half would
    silently destroy evidence and mask the bug in whatever produced it.

    Their job is to be what a test asks: any code that deletes conversation rows must *prove* it
    never leaves one of these, rather than rely on something cleaning up afterwards.
    `droppable_rows` is what makes that provable; these are what check it.
    """
    called = {
        str(call["id"])
        for message in messages
        for call in getattr(message, "tool_calls", None) or []
        if call.get("id") is not None
    }
    return {
        answered
        for answered in (_answered_id(m) for m in messages)
        if answered is not None and answered not in called
    }


def droppable_rows(
    rows: Sequence[tuple[int, AbstractSet[str] | None]], candidates: AbstractSet[int]
) -> set[int]:
    """Narrow `candidates` to the rows that can be deleted without stranding a tool-call pairing.

    Storage may not dispose of a conversation row on its own terms: a tool call and the message
    answering it are one indivisible unit, and deleting either half alone bricks the thread
    (`unmatched_result_ids` explains why the surviving half cannot be healed). The caller that
    deletes rows — age-based retention — chooses its candidates for reasons that know nothing about
    pairing, so the pairing rule is applied once, here rather than there.

    Rows are joined into components by shared call id, in **either** direction: a row is linked to
    every row mentioning an id it mentions, whether as the call or the answer. The relation is
    transitive, which matters for parallel calls — one assistant message carrying three calls links
    to all three answering rows, and those may link on again. A component survives or dies whole.

    **This contracts, it never expands.** A component with even one row outside `candidates` is
    dropped from the answer entirely, rather than pulling its remaining rows in. That direction is
    the safety property: expanding would let an age cutoff reach *forward* and delete a live result
    from a recent turn, whereas contracting can only ever return a subset of what the caller already
    chose — so the worst case is a straddling group surviving one more pass, which is harmless and
    self-correcting.

    **An unreadable row (`None`) makes the whole session undroppable this pass.** Leaving it merely
    undroppable is not enough: it links to nothing, so a partner it *would* have protected stays
    eligible, and the sweep strands exactly the pairing this function exists to protect — reached
    by being careful about the wrong row. Refusing the session is self-correcting, because the next
    pass sees the same rows once somebody has looked at them.

    Args:
        rows: Every row of the session, as `(row_id, call ids)` — not just the candidates. A
            candidate's partner is frequently *not* a candidate (that is precisely the case worth
            catching), so a partial view would report a split component as droppable. `None` in
            place of a set marks a row whose stored shape could not be read (`stored_call_ids`).
        candidates: The row ids the caller wants to delete.

    Returns:
        The subset of `candidates` that is safe to delete; empty when any row was unreadable.
    """
    if unreadable_rows(rows):
        return set()
    # Union-find over row ids, keyed by call id. A dict of representatives is enough at this size
    # (one session's history), and path compression keeps the transitive case honest.
    parent: dict[int, int] = {row_id: row_id for row_id, _ in rows}

    def find(row_id: int) -> int:
        while parent[row_id] != row_id:
            parent[row_id] = parent[parent[row_id]]
            row_id = parent[row_id]
        return row_id

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    first_row_for_call: dict[str, int] = {}
    for row_id, call_ids in rows:
        for call_id in call_ids or ():
            seen = first_row_for_call.setdefault(call_id, row_id)
            union(seen, row_id)

    members: dict[int, set[int]] = {}
    for row_id, _ in rows:
        members.setdefault(find(row_id), set()).add(row_id)
    return {
        row_id for component in members.values() if component <= candidates for row_id in component
    }


def unreadable_rows(rows: Iterable[tuple[int, AbstractSet[str] | None]]) -> list[int]:
    """The ids of rows whose stored shape could not be read — for a caller that wants to say so.

    Separate from `droppable_rows` because the two answer different questions and only one of them
    is a log line. The sweep refuses the session either way; an operator needs the row numbers to
    find out *why*, and a rule that decides what to destroy should not also be deciding what to
    print.
    """
    return [row_id for row_id, call_ids in rows if call_ids is None]
