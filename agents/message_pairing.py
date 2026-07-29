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
