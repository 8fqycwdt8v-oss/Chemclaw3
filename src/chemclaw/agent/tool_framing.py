"""Frame the result of every out-of-process tool call as data before the model reads it.

In-process tools that return retrieved text frame it themselves (`agent/framing.py`); a connector
result comes from another process, so there is no call site to frame it at and the property must be
a middleware. The whole payload is framed rather than chosen fields: the envelope wraps the content
blocks' text and leaves block ids and `structured_content` untouched, and a field list would go
stale and leave sibling fields as unframed channels.

Three treatments:

1. **Framed** — calls answered by a server outside this process, identified by the `SERVED_BY` stamp
   `connectors/transport._stamped` puts on every MCP tool (the same fact
   `agent/audit.py::_served_by` records). A stamp on the tool object that ran cannot disagree with
   what ran.
2. **Defanged, not framed** — a connector failure, a helper's report (`subagent_tool_names()`) and
   every scratchpad verb (`scratchpad_tools()`): text that can spell the closing delimiter but is
   not evidence to cite.
3. **Left alone** — everything else. This is sound only while every in-process tool returning
   model-authored or third-party text neutralises it itself; that is a review rule, not a control.

The stamp is checked before any name, because a connector may declare a name like `read_file` and a
third-party payload must be framed, not merely defanged. (Discovery also refuses such names;
`tests/test_tool_framing.py` holds both.)

`defanged_payload` is exported for in-process tools that return structured results, where
neutralising every string is the treatment available.

Position: outside the audit middleware, so `audit_events.detail` records what the tool returned;
inside the two converters, so this system's own refusals are never framed as third-party data.
"""

from collections.abc import Callable
from enum import Enum
from typing import Any, TypeVar, cast

from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage
from pydantic import BaseModel

from chemclaw.agent.framing import (
    SYSTEM_SPEECH_MARK,
    defang,
    envelope_delimiters,
    frame_untrusted,
    neutralise_marks,
)
from chemclaw.agent.tool_result_shape import (
    cut_short_report,
    empty_result_notice,
    helper_stopped_by,
    returned_nothing,
    rewritten_tool_messages,
)
from chemclaw.agent.tool_result_size import (
    FULL_RESULT_REF_KEY,
    bounded_for_batch,
    full_text,
    kept_in_full,
    original_chars,
    stored_result_ref,
    text_chars,
    was_cut,
)
from chemclaw.connectors.transport import SERVED_BY
from chemclaw.core.config import settings
from chemclaw.core.result_handle import handle_line, handles_resolve
from chemclaw.exhibits.evidence import is_evidence

#: What `defanged_payload` preserves: a payload comes back as the type it went in as.
_Payload = TypeVar("_Payload")


def defanged_payload(payload: _Payload) -> _Payload:
    """`payload` with every string inside it neutralised, and its shape untouched.

    For structured results (`ConnectorJobResult`, a job's `result` dict, a note's frontmatter) where
    `frame_untrusted` has nothing to wrap. Recursive; dict keys are neutralised as well as values,
    since a model reads keys as text too. Pydantic `extra="allow"` extras and set/frozenset members
    are covered. Enum members (including `str` enums), numbers, dates and other non-text values are
    returned as they came.

    A model is rebuilt with `model_copy` (no re-validation, so defanging can never raise a
    `ValidationError`), and `model_fields_set` is restored so the copy differs from the original
    only in its text.
    """
    return cast(_Payload, _defanged(payload))


def _defanged(value: Any) -> Any:
    """The untyped half of `defanged_payload`, where the recursion actually happens.

    `Enum` is tested before `str`, because a `str`-subclass enum would otherwise be downgraded to a
    plain string.
    """
    if isinstance(value, Enum):
        return value
    if isinstance(value, str):
        return defang(value)
    if isinstance(value, BaseModel):
        return _defanged_model(value)
    if isinstance(value, dict):
        return {_defanged(key): _defanged(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_defanged(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_defanged(item) for item in value)
    if isinstance(value, frozenset):
        return frozenset(_defanged(item) for item in value)
    if isinstance(value, set):
        return {_defanged(item) for item in value}
    return value


def _defanged_model(value: BaseModel) -> BaseModel:
    """One model, rebuilt with every field *and* every `extra="allow"` extra neutralised.

    `model_fields_set` is put back to the original's, since `model_copy` marks every updated name as
    set.
    """
    extra = value.__pydantic_extra__ or {}
    copy = value.model_copy(
        update={
            **{name: _defanged(getattr(value, name)) for name in type(value).model_fields},
            **{name: _defanged(item) for name, item in extra.items()},
        }
    )
    copy.__pydantic_fields_set__ = set(value.__pydantic_fields_set__)
    return copy


def served_by(request: Any) -> str:
    """`"<connector>:<tool>"` when an out-of-process server answers this call, else `""`.

    The envelope id and the predicate are one question; the id gives a citation the server and tool
    that produced the span. Reads the `SERVED_BY` stamp from `request.tool.metadata`, the key
    `agent/audit.py::_served_by` also reads.

    `kg.note.mentioned_ids` will read the connector name out of this `id` attribute as if it were a
    note id; that only widens the grounded set, and the fix belongs in `mentioned_ids`.

    `request.tool` is `None` for an unregistered name; here that simply means nothing to frame.
    """
    metadata = getattr(getattr(request, "tool", None), "metadata", None) or {}
    served = metadata.get(SERVED_BY)
    if not isinstance(served, dict):
        return ""
    connector = str(served.get("connector") or "connector")
    return f"{connector}:{request.tool_call['name']}"


def stand_in_notice(request: Any) -> str:
    """This system's warning that the connector answering `request` is a stand-in, else `""`.

    A test double must say so in the result, or the model reads its fixed output as a real
    prediction. Which connectors are stand-ins is the deployment's statement
    (`connector_stand_ins`), not inferred from names. The sentence sits outside the data envelope
    and ends in `SYSTEM_SPEECH_MARK`, because it is this system speaking about the call.
    """
    metadata = getattr(getattr(request, "tool", None), "metadata", None) or {}
    served = metadata.get(SERVED_BY)
    if not isinstance(served, dict):
        return ""
    connector = str(served.get("connector") or "")
    if not connector or connector not in settings.connector_stand_ins_list:
        return ""
    return (
        f"STAND-IN RESULT: on this deployment the {connector!r} connector is a deterministic test "
        "double, not the scientific capability it describes — it returns the same fixed output "
        "whatever the input, so the result below carries no chemical information. Do not use it "
        "as evidence for anything, and if you mention it, tell the chemist plainly that it came "
        f"from a stand-in rather than a model. {SYSTEM_SPEECH_MARK}\n"
    )


def _with_notice(content: Any, notice: str) -> Any:
    """`content` with `notice` in front of it, as its own span outside any envelope."""
    if not notice:
        return content
    if isinstance(content, str):
        return notice + content
    if isinstance(content, list):
        return [{"type": "text", "text": notice}, *content]
    return content


def _rewritten(content: Any, rewrite: Callable[[str], str]) -> Any:
    """Apply `rewrite` to every span of text in a `ToolMessage.content`, preserving its shape.

    `content` is a string (in-process tools) or a list of content blocks (MCP tools). Blocks are
    rebuilt, not mutated, carrying every other key across unchanged. Blocks with no text (images,
    embedded resources) and empty strings are returned as they came.
    """
    if isinstance(content, str):
        return rewrite(content) if content else content
    if isinstance(content, list):
        return [_rewritten_block(block, rewrite) for block in content]
    return content


def _rewritten_block(block: Any, rewrite: Callable[[str], str]) -> Any:
    """One content block with its text span rewritten, or the block unchanged."""
    if not _carries_text(block):
        return block
    if isinstance(block, str):
        return rewrite(block)
    return {**block, "text": rewrite(block["text"])}


def _carries_text(block: Any) -> bool:
    """Whether this block has a non-empty text span — i.e. whether there is anything to rewrite.

    Shared by `_rewritten_block` and `_framed_content` so both agree on which blocks carry text.
    """
    if isinstance(block, str):
        return bool(block)
    return isinstance(block, dict) and isinstance(block.get("text"), str) and bool(block["text"])


def _framed_content(content: Any, origin: str) -> Any:
    """`content` inside **one** envelope naming `origin`, whatever shape it arrived in.

    One envelope per result rather than per block: a per-block envelope adds a constant per block,
    and the block count is unbounded, so the result could grow far past what `bound_tool_results`
    measured. The opening delimiter rides on the first text span and the closing one on the last.
    Every span is still defanged, so a middle span cannot close the envelope early.
    """
    if isinstance(content, str):
        return frame_untrusted(content, note_id=origin) if content else content
    if not isinstance(content, list):
        return content
    spans = [index for index, block in enumerate(content) if _carries_text(block)]
    if not spans:
        # Nothing to frame, and an envelope around nothing is a citation to nothing.
        return content
    opening, closing = envelope_delimiters(origin)
    first, last = spans[0], spans[-1]

    def _rewrite(index: int) -> Callable[[str], str]:
        head = opening if index == first else ""
        tail = closing if index == last else ""
        return lambda text: f"{head}{defang(text)}{tail}"

    return [_rewritten_block(block, _rewrite(index)) for index, block in enumerate(content)]


@wrap_tool_call
async def frame_connector_results(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Wrap an out-of-process tool's result in the data envelope; leave every other result alone.

    **A connector failure is defanged, not framed**: it is a statement about the call, not evidence
    to cite, but its text is still third-party and may carry a forged delimiter. MCP tools never
    raise, so `status="error"` is the only form a connector failure takes. The announcer and audit
    trail run inside this middleware and see the untouched message.

    **A helper's report is defanged, not framed**: it is model prose written after reading framed
    evidence, so it may reproduce the live delimiter (the helper has seen the nonce). Framing it
    would credit this system's own paraphrase as a source.

    **Scratchpad verbs are defanged, not framed**: a helper's `/scratch/` files cross into the
    caller's `files` channel, and `read_file` is in-process, so without this a copied delimiter
    would arrive live
    (`tests/test_subagents.py::test_a_helpers_file_reaches_its_caller_and_is_defanged_when_read`).
    The whole verb set is covered, since `grep` content and `write_file`'s echoed path also carry
    text. The same routing reaches `/skills/` and `/memories/`, neither of which is a citable source
    either.

    Only what the model sees is rewritten; stored files stay pristine. A consequence: an `edit_file`
    whose `old_string` was copied from an escaped read will not match; the model must edit on a span
    it did not copy through a rewrite.
    """
    # Deferred import: `chemclaw_agent` reaches this module's siblings through the agent builder.
    # Both sets are `@cache`d where derived.
    from chemclaw.agent.chemclaw_agent import subagent_tool_names
    from chemclaw.agent.scratchpad import scratchpad_tools

    result = await handler(request)
    # A result only this pass cuts (escaping pushed it over the ceiling) keeps its full text exactly
    # as the inner pass would (`tool_result_size.kept_in_full`); the recorded text is the pre-escape
    # content, which on that path is what the tool returned.
    originals: dict[str, str] = {}

    def _kept(message: ToolMessage, offered: Any, bounded: Any) -> dict[str, Any]:
        """The metadata the delivered message carries, recording a cut only this pass made.

        A result the inner pass already stored names its full text (`RESULT_REF_KEY`), so this pass
        points at that ref rather than storing the bytes twice.
        """
        metadata = dict(message.response_metadata or {})
        if bounded is offered or was_cut(message):
            return metadata
        stored = stored_result_ref(message)
        if stored:
            metadata[FULL_RESULT_REF_KEY] = stored
        else:
            originals[message.tool_call_id] = full_text(message.content)
        return metadata

    # A helper stopped by a turn limit says so before anything it wrote; read off the `Command`
    # before the per-message rewrite (see `tool_result_shape.helper_stopped_by`). `None` otherwise.
    stopped = helper_stopped_by(result)

    def _defanged(message: ToolMessage) -> ToolMessage:
        # Re-bounded after escaping, because escaping can expand text up to 4x (`framing._defang`'s
        # second pass escapes every `<`), and the ceiling applies to what the model is sent. The
        # inner `bound_tool_results` already recorded what the tool returned, so this pass describes
        # that number and does not count the cut twice (`charged_total`/`expanded_from`/`count`).
        # `charged_total` falls back to the size in hand when the inner pass did not cut, since that
        # is then what the tool returned; `expanded_from` converts the kept span back to the tool's
        # own units. See `bounded_content`.
        in_hand = text_chars(message.content)
        escaped = _rewritten(message.content, defang)
        if stopped is not None and isinstance(escaped, str):
            # After the defang, so the added mark is live and any imitation in the helper's text is
            # not; before the bound, so the bound charges it and keeps it. `_kept` records the
            # helper's own content, without the mark.
            escaped = cut_short_report(escaped, stopped)
        bounded = bounded_for_batch(
            request,
            escaped,
            charged_total=original_chars(message) or in_hand,
            expanded_from=in_hand,
            count=original_chars(message) is None,
        )
        return message.model_copy(
            update={"content": bounded, "response_metadata": _kept(message, escaped, bounded)}
        )

    origin = served_by(request)
    if origin:
        notice = stand_in_notice(request)

        def _framed(message: ToolMessage) -> ToolMessage:
            if message.status == "error":
                return _defanged(message)
            if returned_nothing(message):
                # Said, not framed: an envelope around nothing is a citation to nothing, and a
                # bare `""` reads as "nothing found" (`tool_result_shape.returned_nothing`).
                return message.model_copy(update={"content": empty_result_notice()})
            # Re-bounded for the same reason as the defanged branch: `_framed_content` defangs
            # before wrapping, which can expand the text up to 4x. Without this a framed success
            # could exceed the ceiling and be evicted whole by upstream
            # (`test_a_connector_success_survives_the_ceiling_instead_of_being_evicted`).
            #
            # Bounding the framed string is safe: `bounded_for_batch` cuts head-and-tail, so both
            # delimiters survive. Its notice lands inside the envelope, so it must carry the escaped
            # `SYSTEM_SPEECH_MARK`, not the live one (a live mark inside retrieved data is what an
            # attacker would forge). It is escaped while the notice is sized, because escaping
            # afterwards would push the result past the exact ceiling.
            in_hand = text_chars(message.content)
            # The stand-in notice goes in front of the envelope, before the bound, so the ceiling
            # charges it and the head-and-tail cut keeps it (`stand_in_notice`).
            framed = _with_notice(_framed_content(message.content, origin), notice)
            # Same `charged_total`/`expanded_from`/`count` arithmetic as the defanged branch above.
            bounded = bounded_for_batch(
                request,
                framed,
                mark=neutralise_marks(SYSTEM_SPEECH_MARK),
                charged_total=original_chars(message) or in_hand,
                expanded_from=in_hand,
                count=original_chars(message) is None,
            )
            return message.model_copy(
                update={"content": bounded, "response_metadata": _kept(message, framed, bounded)}
            )

        return await kept_in_full(
            rewritten_tool_messages(result, _framed), originals, str(request.tool_call["name"])
        )
    name = request.tool_call["name"]
    if name in subagent_tool_names() or name in scratchpad_tools():
        return await kept_in_full(rewritten_tool_messages(result, _defanged), originals, str(name))
    return result


#: What a bracketed handle's opening bracket becomes when a *tool* wrote it: the character
#: reference for `⟨`, which a reader still sees and which no longer has the handle's shape.
_ESCAPED_BRACKET = "&#10216;"


def _without_forged_handles(content: Any) -> Any:
    """`content` with every handle-shaped run a tool wrote escaped, so only the stamp is one.

    A tool could write `⟨r:<another result's hex>⟩` naming a real result of the same conversation;
    every such run is escaped, so a model asked to copy "the handle" finds exactly one.
    """
    return _rewritten(content, lambda text: text.replace("⟨r:", f"{_ESCAPED_BRACKET}r:"))


def _with_handle(message: ToolMessage, *, stamp: bool = True) -> ToolMessage:
    """`message` ending in its handle line, forged ones escaped; unstamped where nothing was stored.

    The escape runs on every result, stamped or not. `stamp` false leaves the handle off a result no
    binding may name (see `stamp_result_handles`).
    """
    content = _without_forged_handles(message.content)
    ref = stored_result_ref(message)
    if not stamp or not ref or message.status == "error":
        if content == message.content:
            return message
        return message.model_copy(update={"content": content})
    line = handle_line(ref)
    if isinstance(content, str):
        stamped: Any = content + line
    elif isinstance(content, list):
        stamped = [*content, {"type": "text", "text": line}]
    else:
        return message
    return message.model_copy(update={"content": stamped})


@wrap_tool_call
async def stamp_result_handles(request: Any, handler: Callable[[Any], Any]) -> Any:
    """End every stored result with `⟨r:<12 hex>⟩`, the handle an artefact binding names it by.

    Outside the framing, so the handle is this system's last line rather than part of the evidence;
    every tool-written handle is escaped first (`_without_forged_handles`), so the stamp is the only
    one.

    Stamped only where the full text was stored (`tool_result_size.RESULT_REF_KEY`), and only where
    a binding could resolve it: not when the deployment's bindings cannot read the store
    (`core.result_handle.handles_resolve`), and not on a result that is not evidence
    (`exhibits.evidence.is_evidence`). Nothing re-bounds after this, so a stamped result may exceed
    its share of the ceiling by one handle line.
    """
    stamp = handles_resolve() and is_evidence(str(request.tool_call["name"]))
    return rewritten_tool_messages(
        await handler(request), lambda message: _with_handle(message, stamp=stamp)
    )
