"""The one rule a stored conversation must satisfy: every tool call carries its result.

Anthropic (and every other tool-calling API) rejects a thread in which a `tool_use` block is not
answered by a matching `tool_result` — "tool_use ids were found without tool_result blocks". That
makes an unmatched call uniquely dangerous in *durable* history: it is not one bad turn, it is a
poison pill replayed on every subsequent turn, so a session that acquires one is bricked until
somebody edits the database.

A turn can end between writing the call and writing the result in more ways than a rollback can
cover — a client disconnect (`service.runner` handles that one), but also a pod eviction, an OOM
kill, or a `SIGKILL`, none of which run any Python cleanup at all. So the invariant is enforced
where it can never be skipped: on the way *out* of durable storage.

Pure functions over MAF `Message`s, with no I/O, so both the storage layer and the harness
regression test can assert the same rule rather than restating it.
"""

import copy
from collections.abc import Sequence
from collections.abc import Set as AbstractSet

from agent_framework import Message

_CALL = "function_call"
_RESULT = "function_result"


def unmatched_call_ids(messages: Sequence[Message]) -> set[str]:
    """Return the `call_id`s of function calls that no function result answers.

    Order-independent on purpose: a result is a valid answer to its call wherever it sits in the
    list, so this reports genuinely unanswered calls rather than merely out-of-order ones.
    """
    answered = {
        content.call_id
        for message in messages
        for content in message.contents
        if content.type == _RESULT and content.call_id is not None
    }
    # A call with no `call_id` is skipped rather than reported: nothing can be matched to it, so it
    # is also nothing this function could name for a caller to strip. Real tool calls always carry
    # one — it is what the API pairs on.
    return {
        content.call_id
        for message in messages
        for content in message.contents
        if content.type == _CALL and content.call_id is not None and content.call_id not in answered
    }


def unmatched_result_ids(messages: Sequence[Message]) -> set[str]:
    """Return the `call_id`s of function *results* that no function call accounts for.

    The mirror of `unmatched_call_ids`, and the asymmetry between them is the whole reason this
    exists. `unmatched_call_ids` reports unanswered calls and `strip_call_ids` removes them, so an
    orphaned call is detected and healed on every read. Nothing detects an orphaned **result**: the
    repair filters on `type == "function_call"` only, so a `tool_result` whose `tool_use` is gone is
    invisible to it — and the API rejects that thread exactly as hard as the converse. A stranded
    result is therefore a bricked session with *no* self-heal path.

    Deliberately **not** wired into the read-time repair (D-145). Stripping a stranded result would
    silently destroy evidence and, worse, would mask a bug in whatever produced it. Its job is to be
    the assertion: any code that deletes conversation rows must prove it never leaves one of these,
    rather than rely on something cleaning up afterwards.
    """
    called = {
        content.call_id
        for message in messages
        for content in message.contents
        if content.type == _CALL and content.call_id is not None
    }
    return {
        content.call_id
        for message in messages
        for content in message.contents
        if content.type == _RESULT and content.call_id is not None and content.call_id not in called
    }


def droppable_rows(rows: Sequence[tuple[int, Message]], candidates: AbstractSet[int]) -> set[int]:
    """Narrow `candidates` to the rows that can be deleted without stranding a tool-call pairing.

    Storage may not dispose of a conversation row on its own terms: a `function_call` and the
    `function_result` answering it are one indivisible unit, and deleting either half alone bricks
    the session (`unmatched_result_ids` explains why the surviving half cannot be healed). Both
    callers that delete rows — durable compaction and age-based retention — choose their candidates
    for reasons that know nothing about pairing, so the pairing rule is applied once, here.

    Rows are joined into components by shared `call_id`, in **either** direction: a row is linked to
    every row mentioning an id it mentions, whether as the call or the result. The relation is
    transitive, which matters for parallel calls — one assistant message carrying three calls links
    to all three result rows, and those may link on again. A component survives or dies whole.

    **This contracts, it never expands.** A component with even one row outside `candidates` is
    dropped from the answer entirely, rather than pulling its remaining rows in. That direction is
    the safety property: expanding would let an age cutoff reach *forward* and delete a live result
    from a recent turn, whereas contracting can only ever return a subset of what the caller already
    chose — so the worst case is a straddling group surviving one more pass, which is harmless and
    self-correcting.

    Args:
        rows: Every row of the session, as `(row_id, message)` — not just the candidates. A
            candidate's partner is frequently *not* a candidate (that is precisely the case worth
            catching), so a partial view would report a split component as droppable.
        candidates: The row ids the caller wants to delete.

    Returns:
        The subset of `candidates` that is safe to delete.
    """
    # Union-find over row ids, keyed by call_id. A dict of representatives is enough at this size
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
    for row_id, message in rows:
        for content in message.contents:
            call_id = content.call_id if content.type in (_CALL, _RESULT) else None
            if call_id is None:
                continue
            seen = first_row_for_call.setdefault(call_id, row_id)
            union(seen, row_id)

    members: dict[int, set[int]] = {}
    for row_id, _ in rows:
        members.setdefault(find(row_id), set()).add(row_id)
    return {
        row_id for component in members.values() if component <= candidates for row_id in component
    }


def calls_without_adjacent_results(messages: Sequence[Message]) -> set[str]:
    """Return `call_id`s whose result is not in the *immediately following* message.

    The stricter, on-the-wire form of the rule: the API does not merely require that a result
    exists somewhere, it requires it in the very next block — "tool_use ids were found without
    tool_result blocks **immediately after**". The two checks differ exactly where duplicated
    history does: replaying a call the transcript already answered leaves a second, unanswered copy
    that `unmatched_call_ids` still considers satisfied, because the id does appear answered once.

    Use this to validate what is about to be sent; use `unmatched_call_ids` to decide what is safe
    to keep in storage, where a merely out-of-order pair is intact history and must not be deleted.
    """
    missing: set[str] = set()
    for index, message in enumerate(messages):
        called = {c.call_id for c in message.contents if c.type == _CALL and c.call_id is not None}
        if not called:
            continue
        following = messages[index + 1] if index + 1 < len(messages) else None
        answered = (
            {c.call_id for c in following.contents if c.type == _RESULT}
            if following is not None
            else set()
        )
        missing |= called - answered
    return missing


def strip_call_ids(message: Message, call_ids: set[str]) -> Message | None:
    """Return `message` without the calls in `call_ids`, or `None` if nothing is left of it.

    Returns the *same object* when there is nothing to strip, so a caller can use identity to tell
    "untouched" from "rewritten" — which a storage layer needs in order to update only the rows
    that actually changed.

    Only the offending content item is dropped, not the whole message: an assistant turn commonly
    carries prose *and* a tool call, and discarding the prose would silently rewrite what the
    assistant said. A message left with no content at all yields `None`, since an empty message is
    itself a malformed block.
    """
    contents = [
        content
        for content in message.contents
        if not (content.type == _CALL and content.call_id in call_ids)
    ]
    if len(contents) == len(message.contents):
        return message
    if not contents:
        return None
    # Shallow-copy so `author_name`/`message_id`/`additional_properties` ride along — rebuilding
    # via `Message(role=..., contents=...)` would quietly drop them.
    trimmed = copy.copy(message)
    trimmed.contents = contents
    return trimmed


def strip_unmatched_calls(messages: Sequence[Message]) -> list[Message]:
    """Return `messages` with every unanswered function call removed."""
    orphans = unmatched_call_ids(messages)
    if not orphans:
        return list(messages)  # the overwhelmingly common case: no copying, no allocation churn
    stripped = (strip_call_ids(message, orphans) for message in messages)
    return [message for message in stripped if message is not None]
