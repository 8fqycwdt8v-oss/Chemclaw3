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


# Every content type this converter knows how to carry across. A stored row holding anything else is
# refused rather than converted, which is what `043_session_message_shape.sql` and this module's own
# docstring have always claimed and what the code did not do: unknown parts were simply not matched
# by any branch, so they vanished into an empty string and the row was stamped as converted. A
# migration that silently drops what it does not recognise is the one thing an irreversible pass
# must not be, and the drop was invisible precisely because the result still looked like a message.
_KNOWN_CONTENT = frozenset({"text", "function_call", "function_result"})


def _reject_unknown_content(contents: list[dict[str, Any]]) -> None:
    """Stop on a content type this converter has no example of, naming it.

    The alternative is guessing at data that outlives the guess. A refused row keeps its `maf` stamp
    and stays readable through `session_store.message_from_row`, so refusing costs the conversation
    nothing and buys an operator a row number and a type name.
    """
    unknown = sorted({str(c.get("type")) for c in contents if isinstance(c, dict)} - _KNOWN_CONTENT)
    if unknown:
        raise UnconvertibleMessage(
            f"stored message holds content type(s) {unknown} this converter has never written"
        )


def _tool_calls(contents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The assistant's tool calls, in LangChain's `{name, args, id}` shape.

    The stored `arguments` is the same thing LangChain calls `args`, so this is mostly a rename;
    `_arguments` handles the string form the streaming path produces. A call whose arguments will
    not parse degrades to `{}` rather than raising,
    because a half-streamed argument blob is not something to reconstruct months later; the call is
    still visible in the transcript with its name and id.
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

    Both forms are in the table: the decoded object for a call that arrived whole, and the raw JSON
    *string* for one assembled from streamed fragments. Only the first was read, so every streamed
    call in the archive converted to `args: {}` — the arguments a reviewer asks "what was this
    run with" about, replaced by nothing, in the irreversible pass.

    A string that will not parse still degrades to `{}` rather than raising: it is a half-streamed
    fragment, the call is still visible with its name and id, and refusing the whole row over an
    argument blob nobody can reconstruct would cost the conversation to save a detail that is
    already gone.
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

    `tool_call_id` is required rather than best-effort: a `ToolMessage` with no id is a result
    answering nothing, which every provider rejects as a malformed exchange — the same rule the
    LangGraph gates obey when they refuse a call.

    **A row answering several calls is refused, not truncated.** Parallel tool calls are answered by
    one stored `tool` row holding one `function_result` per call, and this returns a single
    `ToolMessage` — so taking the first and returning silently destroyed the rest, irreversibly, in
    the one pass whose own docstring calls itself the irreversible step. Worse than the loss is what
    it leaves behind: the surviving assistant message still carries all three calls, so the thread
    acquires unanswered `tool_use` blocks that a provider rejects outright, which is the poison pill
    `agent/message_pairing.py` exists to keep out of a conversation.

    Refusing costs nothing a chemist can see. A refused row keeps its `maf` stamp and stays fully
    readable through `session_store.message_from_row`, which reads both shapes — so the conversation
    renders exactly as before while the pass names the row for an operator.
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


if __name__ == "__main__":
    # Its own entrypoint rather than a step inside `core.migrate`, because the kernel imports no
    # other subpackage — `tests/test_layering.py` caught exactly that when this was first wired
    # there, and it was right: a schema applier that reaches into layer 1 to convert its data is
    # the dependency direction this tree is arranged to prevent.
    #
    # Ordered after the schema by whoever runs them (`make db-migrate`, the chart's hook Job), for
    # the obvious reason: the stamp's column has to exist before anything writes it.
    outcome = asyncio.run(convert_stored_messages())
    print(f"converted {outcome.converted} stored message(s)")
    if outcome.refused:
        ids = ", ".join(str(row_id) for row_id in outcome.refused[:20])
        print(
            f"refused {len(outcome.refused)} row(s) (ids: {ids}) — these keep their original shape,"
            " stay readable, and need a look"
        )
