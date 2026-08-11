"""Convert a stored MAF message payload into a LangChain one (M6, D-2026-08-10).

`session_messages.message` holds `agent_framework.Message.to_dict()` verbatim — 008's DDL says so
explicitly, and says why: "the store does not interpret it, so a MAF message-shape change is a
value change, not a schema change". That property is what makes this migration a *data* change
rather than a schema change, and it is also why the conversion has to live somewhere: the rows are
a real conversation history that chemists can still read, so they cannot simply be dropped when the
engine changes.

**Two halves, and the split is the point.** `to_langchain` is pure — a dict in, a message out, no
database, no settings, no clock — so the decision about *what* each row becomes can be tested
exhaustively against payloads MAF itself produced, with no Postgres in reach.
`convert_stored_messages` is the pass that decides *which* rows to rewrite, and it is resumable and
refusal-tolerant precisely because rewriting rows is the one irreversible step. Keeping them in one
module
rather than two is deliberate: they are read together, and a converter whose caller lives elsewhere
invites a second caller that converts differently.

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

import logging
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_to_dict,
)
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)

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


@dataclass(frozen=True, slots=True)
class ConversionOutcome:
    """What one conversion pass did, and what it refused.

    `refused` carries row ids rather than a count alone: the whole reason this pass can refuse is
    that a stored message has no example to check a guess against, and the only useful next step is
    to go and look at the row.
    """

    converted: int
    refused: tuple[int, ...]

    def is_complete(self) -> bool:
        """Whether every row this pass saw was converted."""
        return not self.refused


# One session's rows are read whole because the conversion is per row and order-free; the batch
# bounds *memory*, not correctness, so a value that keeps a page of rows comfortably in hand is the
# whole requirement. It is a parameter rather than a setting because nobody tunes a one-off.
_BATCH = 500

_SELECT_MAF = (
    "SELECT id, message FROM session_messages "
    f"WHERE message_shape = '{MAF_SHAPE}' ORDER BY id LIMIT %s"
)
_MARK_CONVERTED = (
    f"UPDATE session_messages SET message = %s, message_shape = '{LANGCHAIN_SHAPE}' WHERE id = %s"
)


async def convert_stored_messages(*, batch: int = _BATCH) -> ConversionOutcome:
    """Rewrite every MAF-shaped row into LangChain shape, stamping each as it goes.

    The store's own DSN resolver and pooled-connection helper are imported here rather than at
    module scope, and not only to break the cycle the read path introduced: `_session_dsn` is "one
    resolver for all three classes so they can never point at different databases", and a
    conversion pass that resolved its own DSN could rewrite a different database from the one the
    provider reads — the single worst thing this module could do. Deferring the import keeps the
    pure half genuinely pure, which is what its own docstring above promises.

    **Resumable by construction, which is what makes an irreversible step survivable.** The pass
    selects only rows still stamped `maf`, so running it twice converts nothing twice and an
    interrupted run simply continues where it stopped. Nothing is deleted and nothing is rewritten
    in place without its stamp changing in the same statement, so there is no window in which a row
    holds one shape and claims the other.

    A row the converter refuses is **left exactly as it was**, stamp included, and reported. The
    alternative — aborting the whole pass on the first refusal — would make one unreadable message
    block the conversion of every row after it, which is the opposite of what a resumable pass is
    for. The caller decides whether a refusal is worth stopping over; this reports it.

    Args:
        batch: How many rows to hold in memory at once. Bounds memory, not correctness.

    Returns:
        What was converted and which rows were refused.
    """
    from chemclaw.agent.session_store import _session_connection, _session_dsn

    converted = 0
    refused: list[int] = []
    async with _session_connection(_session_dsn()) as conn:
        while True:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_MAF, (batch + len(refused),))
                rows = [(int(row[0]), row[1]) for row in await cur.fetchall()]
            pending = [(row_id, payload) for row_id, payload in rows if row_id not in set(refused)]
            if not pending:
                return ConversionOutcome(converted, tuple(refused))
            updates = []
            for row_id, payload in pending:
                try:
                    message = to_langchain(payload)
                except UnconvertibleMessage:
                    logger.warning("session_messages row %d could not be converted", row_id)
                    refused.append(row_id)
                    continue
                updates.append((Jsonb(message_to_dict(message)), row_id))
            if updates:
                async with conn.cursor() as cur:
                    await cur.executemany(_MARK_CONVERTED, updates)
                await conn.commit()
                converted += len(updates)
