"""Convert a stored MAF message payload into a LangChain one.

`session_messages.message` holds MAF `Message.to_dict()` payloads verbatim; they are real history
chemists still read, so they are converted rather than dropped. `to_langchain` is pure (dict in,
message out) and testable without Postgres; `convert_stored_messages` is the resumable pass that
rewrites rows. Each row is stamped with the shape it holds and both shapes read, so a non-atomic
rollout is safe. The pre-conversion payload is kept in `message_original` by the same statement
that overwrites `message`, so a rollback is one statement
(D-2026-08-27-a-conversion-that-cannot-be-rolled-back-is-not-a-pre-upgrade-step):

    UPDATE session_messages
       SET message = message_original, message_shape = 'maf', message_original = NULL
     WHERE message_original IS NOT NULL;

A content type this system never wrote raises rather than being coerced: there is no example to
check a conversion against.
"""

import asyncio
import json
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

# The stamp naming which shape a row's `message` holds. Absent means MAF, so historical rows need
# no rewrite to gain one.
MAF_SHAPE = "maf"
LANGCHAIN_SHAPE = "langchain"


class UnconvertibleMessage(ValueError):
    """A stored message this converter will not guess at.

    Its own type so a migration can count and name the rows rather than swallow a `ValueError`.
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
    _reject_unknown_content(contents)
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


# Every content type this converter carries across; a row holding anything else is refused rather
# than silently dropped from an irreversible pass.
_KNOWN_CONTENT = frozenset({"text", "function_call", "function_result"})


def _reject_unknown_content(contents: list[dict[str, Any]]) -> None:
    """Stop on a content type this converter has no example of, naming it.

    A refused row keeps its `maf` stamp and stays readable through `session_store.message_from_row`.
    """
    unknown = sorted({str(c.get("type")) for c in contents if isinstance(c, dict)} - _KNOWN_CONTENT)
    if unknown:
        raise UnconvertibleMessage(
            f"stored message holds content type(s) {unknown} this converter has never written"
        )


def _tool_calls(contents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The assistant's tool calls, in LangChain's `{name, args, id}` shape.

    Arguments that will not parse degrade to `{}`; the call stays visible with its name and id.
    """
    calls = []
    for content in contents:
        if content.get("type") != "function_call":
            continue
        calls.append(
            {
                "name": str(content.get("name", "")),
                "args": _arguments(content.get("arguments")),
                "id": str(content.get("call_id", "")),
            }
        )
    return calls


def _arguments(arguments: Any) -> dict[str, Any]:
    """A stored call's arguments as a mapping, parsing the string form rather than discarding it.

    Streamed calls were stored as a raw JSON string, whole ones as an object. An unparseable string
    (a half-streamed fragment) degrades to `{}` rather than refusing the row.
    """
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _tool_message(contents: list[dict[str, Any]]) -> ToolMessage:
    """The tool's result, carrying the call id it answers.

    `tool_call_id` is required: a result answering nothing is a malformed exchange. A row answering
    several parallel calls is refused rather than truncated, since a single `ToolMessage` would
    strand the other calls; the refused row stays readable in MAF shape.
    """
    results = [content for content in contents if content.get("type") == "function_result"]
    if not results:
        raise UnconvertibleMessage("stored tool message holds no function_result")
    if len(results) > 1:
        answered = ", ".join(str(content.get("call_id", "?")) for content in results)
        raise UnconvertibleMessage(
            f"stored tool message answers {len(results)} calls ({answered}) and one LangChain "
            "ToolMessage answers one — converting it would destroy the rest"
        )
    call_id = str(results[0].get("call_id", ""))
    if not call_id:
        raise UnconvertibleMessage("stored tool result has no call_id to answer")
    return ToolMessage(content=_result_text(results[0]), tool_call_id=call_id)


def _result_text(content: dict[str, Any]) -> str:
    """A function result as text — `result` when it is a string, else its rendered `items`.

    Preferring the string keeps a plain answer byte-identical; `items` makes a structured result
    readable.
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

    `refused` carries row ids so an operator can go and look. `converted` counts rows this pass
    changed, not rows it attempted.
    """

    converted: int
    refused: tuple[int, ...]


# Rows held in memory at once; bounds memory, not correctness.
_BATCH = 500

_SELECT_MAF = (
    "SELECT id, message FROM session_messages "
    f"WHERE message_shape = '{MAF_SHAPE}' ORDER BY id LIMIT %s"
)
# The original is copied from the column the same statement overwrites, so it is exactly the bytes
# being lost. `AND message_shape = 'maf'` makes an overlapping second pass (no advisory lock is
# taken) a no-op; without it the loser would store already-converted data in `message_original`.
_MARK_CONVERTED = (
    "UPDATE session_messages SET message_original = message, message = %s, "
    f"message_shape = '{LANGCHAIN_SHAPE}' WHERE id = %s AND message_shape = '{MAF_SHAPE}'"
)


async def convert_stored_messages(*, batch: int = _BATCH) -> ConversionOutcome:
    """Rewrite every MAF-shaped row into LangChain shape, stamping each as it goes.

    Uses the store's own DSN resolver (imported lazily to keep the pure half pure) so it cannot
    rewrite a different database from the one the provider reads. Resumable: only rows still stamped
    `maf` are selected, and each UPDATE changes the stamp and preserves the original together, so it
    can run as a `post-upgrade` hook. A refused row is left as it was and reported rather than
    aborting the pass.

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
                    # `rowcount`, not `len(updates)`: a row a concurrent pass converted first
                    # matches nothing and is
                    # not this pass's work.
                    converted += cur.rowcount
                await conn.commit()


if __name__ == "__main__":
    # Its own entrypoint rather than a step inside `core.migrate`, which imports no other
    # subpackage.
    # Run after the schema: in the chart the DDL is `pre-upgrade` (additive, safe for the running
    # release) and this pass is `post-upgrade` (it rewrites rows the previous release still serves).
    outcome = asyncio.run(convert_stored_messages())
    print(f"converted {outcome.converted} stored message(s)")
    if outcome.refused:
        ids = ", ".join(str(row_id) for row_id in outcome.refused[:20])
        print(
            f"refused {len(outcome.refused)} row(s) (ids: {ids}) — these keep their original shape,"
            " stay readable, and need a look"
        )
