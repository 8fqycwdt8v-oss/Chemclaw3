"""The one rule a stored conversation must satisfy: every tool call carries its result.

Tool-calling APIs reject a thread with an unanswered `tool_use`, on every later turn. Nothing here
heals such a thread; deepagents' `PatchToolCallsMiddleware` does that upstream (pinned in
`tests/test_upstream_surface.py`). What is here:

- **A guard on deletion.** `droppable_rows`, used by `durable/retention.py`, keeps an age cutoff
  from taking one half of a pair. It contracts rather than expands.
- **Assertions.** `unmatched_call_ids`, `unmatched_result_ids` and
  `calls_without_adjacent_results` let tests prove that code which deletes or assembles messages
  (including `agent/compaction.py`) strands nothing. They report and never repair.

`droppable_rows` takes call ids, read from either stored shape by `stored_call_ids`, so the
deletion path imports no framework types.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Any

from langchain_core.messages import BaseMessage
from networkx.utils import UnionFind

# Imported rather than restated so one rule decides what a row is.
from chemclaw.agent.message_migration import LANGCHAIN_SHAPE

logger = logging.getLogger(__name__)

# MAF's content-type discriminators, still carried by unconverted stored rows. They decide which
# rows the retention sweep may delete, so a wrong value silently strands pairings.
_MAF_CALL = "function_call"
_MAF_RESULT = "function_result"


def stored_call_ids(payload: Mapping[str, Any], shape: str | None = None) -> frozenset[str] | None:
    """The tool-call ids one stored `session_messages.message` row mentions, in either direction.

    The `message_shape` stamp decides the shape (MAF `{"role", "contents"}` or LangChain
    `{"type", "data"}`), as in `session_store.message_from_row`; an unstamped historical row falls
    back to the payload. Returns `None` for a payload matching neither shape (including a
    non-mapping):
    unreadable is not the same as "no ids", and treating it so would make the row droppable.
    """
    if not isinstance(payload, Mapping):
        # `message` is bare `jsonb`, so a non-mapping is storable; return `None` rather than raise,
        # so the
        # retention sweep's per-session unreadable-row skip handles it.
        return None
    if shape == LANGCHAIN_SHAPE:
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

    Calls from `tool_calls`, answers from `tool_call_id`: a component is joined by either.
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

    Read with `getattr` because these functions walk mixed `BaseMessage` lists.
    """
    answered = getattr(message, "tool_call_id", None)
    return str(answered) if answered else None


def calls_without_adjacent_results(messages: Sequence[BaseMessage]) -> set[str]:
    """Return the ids of tool calls whose answer is not in the *immediately following* message.

    The on-the-wire rule, for validating what is about to be sent; stricter than
    `unmatched_call_ids` where history is duplicated. "Immediately after" is the contiguous run of
    tool messages after the call, since a parallel batch serializes into one user message. Not the
    storage rule: an out-of-order pair is intact history.
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
        answered: set[str] = set()
        for following in messages[index + 1 :]:
            next_id = _answered_id(following)
            if next_id is None:
                break
            answered.add(next_id)
        missing |= called - answered
    return missing


def unmatched_call_ids(messages: Sequence[BaseMessage]) -> set[str]:
    """Return the ids of tool calls that no tool message answers.

    Order-independent: an answer is valid wherever it sits in the list.
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

    The mirror of `unmatched_call_ids`. Both are assertions, never repairs (D-145): stripping either
    half would destroy evidence and mask the bug that produced it.
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

    Rows are joined into components by shared call id in either direction, transitively (parallel
    calls), and a component survives or dies whole. This contracts and never expands: a component
    with any row outside `candidates` is kept entirely, so the worst case is it survives one more
    sweep. Any unreadable row (`None`) makes the whole session undroppable this pass, since it might
    be the partner of a candidate.

    Args:
        rows: Every row of the session as `(row_id, call ids)`, not just the candidates, since a
            candidate's partner is often not one. `None` marks an unreadable row.
        candidates: The row ids the caller wants to delete.

    Returns:
        The subset of `candidates` that is safe to delete; empty when any row was unreadable.
    """
    if unreadable_rows(rows):
        return set()
    # Union-find over row ids, keyed by call id. Seeded with every row, because a row mentioning no
    # call id is its own component and must be droppable on its own terms.
    components = UnionFind(row_id for row_id, _ in rows)
    first_row_for_call: dict[str, int] = {}
    for row_id, call_ids in rows:
        for call_id in call_ids or ():
            components.union(first_row_for_call.setdefault(call_id, row_id), row_id)

    return {
        row_id
        for component in components.to_sets()
        if component <= candidates
        for row_id in component
    }


def unreadable_rows(rows: Iterable[tuple[int, AbstractSet[str] | None]]) -> list[int]:
    """The ids of rows whose stored shape could not be read — for a caller that wants to say so.

    Separate from `droppable_rows` so the rule deciding deletion does not also decide logging.
    """
    return [row_id for row_id, call_ids in rows if call_ids is None]
