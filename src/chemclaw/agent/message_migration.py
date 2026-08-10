"""Convert a stored MAF message payload into a LangChain one (M6, D-2026-08-10).

`session_messages.message` holds `agent_framework.Message.to_dict()` verbatim — 008's DDL says so
explicitly, and says why: "the store does not interpret it, so a MAF message-shape change is a
value change, not a schema change". That property is what makes this migration a *data* change
rather than a schema change, and it is also why the conversion has to live somewhere: the rows are
a real conversation history that chemists can still read, so they cannot simply be dropped when the
engine changes.

**This module is pure.** No database, no settings, no clock — a dict in, a dict out. The one
irreversible step in the whole migration is rewriting rows, so the part that decides *what* each row
becomes is kept where it can be exhaustively tested without a Postgres, and the part that decides
*which* rows to rewrite is somewhere else entirely.

**Why a shape version rather than an in-place rewrite.** A row is stamped with the shape it holds,
and both shapes read. Two reasons, and the second is the one that matters:

- a rollout is not atomic, so during it some rows are old and some are new;
- if the conversion turns out to be wrong for some message nobody anticipated, an unversioned
  in-place rewrite has already destroyed the evidence. Versioned rows keep the original readable
  until the conversion has been trusted on real data for a while.

**What is deliberately not converted.** MAF's content types outnumber the five this system stores
by about four to one (`Content.from_hosted_file`, `from_oauth_consent_request`,
`from_image_generation_tool_call`, …). Converting a type Chemclaw has never written would be
guessing at a shape with no example to check against, so an unknown content type raises rather than
being silently coerced into text — a message that arrives at the model subtly wrong is worse than a
migration that stops and says which row it could not read.
"""

from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

# The stamp that says which shape a row's `message` column holds. Absent means MAF, because every
# row written before this migration has no stamp and rewriting them all to add one is the very
# rewrite the version exists to avoid.
MAF_SHAPE = "maf"
LANGCHAIN_SHAPE = "langchain"


class UnconvertibleMessage(ValueError):
    """A stored message this converter will not guess at.

    Its own type so a migration can count them, name the rows, and stop — rather than a bare
    `ValueError` that a caller might reasonably swallow.
    """


def to_langchain(payload: dict[str, Any]) -> BaseMessage:
    """Convert one stored MAF `Message.to_dict()` payload into a LangChain message.

    Args:
        payload: The `message` column of one `session_messages` row, in MAF shape.

    Returns:
        The equivalent `BaseMessage`.

    Raises:
        UnconvertibleMessage: The payload holds a role or a content type this system has never
            written, so there is no example to check a conversion against.
    """
    role = str(payload.get("role", ""))
    contents = list(payload.get("contents") or [])
    text = "".join(c.get("text", "") for c in contents if c.get("type") == "text")

    if role == "user":
        return HumanMessage(content=text)
    if role == "system":
        return SystemMessage(content=text)
    if role == "assistant":
        return AIMessage(content=text, tool_calls=_tool_calls(contents))
    if role == "tool":
        return _tool_message(contents)
    raise UnconvertibleMessage(f"stored message has unknown role {role!r}")


def _tool_calls(contents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The assistant's tool calls, in LangChain's `{name, args, id}` shape.

    MAF stores `arguments` as a decoded object, which is the same thing LangChain calls `args`, so
    this is a rename rather than a parse. A call whose arguments were stored as a JSON *string* —
    which the streaming path can produce — is passed through as `{}` rather than parsed here,
    because a half-streamed argument blob is not something to reconstruct months later; the call is
    still visible in the transcript with its name and id.
    """
    calls = []
    for content in contents:
        if content.get("type") != "function_call":
            continue
        arguments = content.get("arguments")
        calls.append(
            {
                "name": str(content.get("name", "")),
                "args": arguments if isinstance(arguments, dict) else {},
                "id": str(content.get("call_id", "")),
            }
        )
    return calls


def _tool_message(contents: list[dict[str, Any]]) -> ToolMessage:
    """The tool's result, carrying the call id it answers.

    `tool_call_id` is required rather than best-effort: a `ToolMessage` with no id is a result
    answering nothing, which every provider rejects as a malformed exchange — the same rule the
    LangGraph gates obey when they refuse a call.
    """
    for content in contents:
        if content.get("type") != "function_result":
            continue
        call_id = str(content.get("call_id", ""))
        if not call_id:
            raise UnconvertibleMessage("stored tool result has no call_id to answer")
        return ToolMessage(content=_result_text(content), tool_call_id=call_id)
    raise UnconvertibleMessage("stored tool message holds no function_result")


def _result_text(content: dict[str, Any]) -> str:
    """A function result as text — `result` when it is a string, else its rendered `items`.

    MAF stores both: `result` is whatever the tool returned, and `items` is the same value already
    rendered into content parts. Preferring the string keeps a plain answer byte-identical;
    falling back to `items` is what makes a structured result readable at all.
    """
    result = content.get("result")
    if isinstance(result, str):
        return result
    items = content.get("items") or []
    rendered = "".join(item.get("text", "") for item in items if item.get("type") == "text")
    return rendered or str(result if result is not None else "")
