"""Reading a turn's streamed updates: tool-call reassembly and the approval prompt.

Everything here is a pure function of the provider-shaped objects `chemclaw.api.runner` receives
from `agent.run` — no ambient state, no session, no config. That is why it lives beside the runner
rather than inside it: the runner's own module is a lifecycle (contextvars, an `AsyncExitStack`, a
rollback), and this is the one part of the per-turn path that can be exercised by handing it a
content object and comparing the events that come back.

Duck-typed throughout, deliberately. MAF's function-call and function-result content classes are
not stable top-level exports and their shape varies by version, so these match on structure (a
`call_id`/`arguments` pair, a result-bearing attribute) rather than importing a concrete type.
"""

import json
from collections.abc import Mapping
from typing import Any

from chemclaw.api.events import Event, ToolCallEvent, ToolResultEvent

# How many characters of a tool call's arguments the trace event carries — enough to see *what*
# was called without streaming a whole evidence payload to the UI (mirrors the audit trail
# truncation).
_ARG_PREVIEW_CHARS = 200


class ToolCallTrace:
    """Reassemble a streamed function call, so `tool_call` can carry the arguments it promises.

    A streamed call does not arrive as one object. The provider sends the *name* first, on a
    content whose `arguments` is still empty, and then streams the argument JSON as fragments on
    further contents that carry only the `call_id` — no name. Reading name-and-arguments off a
    single content, as this did, therefore matched exactly the one content that never has any
    arguments, and skipped every fragment for want of a name: `ToolCallEvent.arguments` was empty
    on every call ever emitted, and could not have been anything else (D-138). The field is
    documented as "a short argument preview" and read by the UI trace, so this was a promise the
    stream never kept.

    Fragments for one call arrive contiguously, so a call is complete once an update goes by
    without adding to it — that is the flush condition, and it needs no knowledge of which
    content type terminates a call. The event therefore lands slightly later than before: after
    the arguments rather than after the name. That is the more truthful order anyway, because a
    tool cannot run before its arguments are complete.

    **That flush condition was still one step too late** (D-159). For a streamed call the next
    update to arrive is the one carrying the call's *result* — the provider finishes the argument
    stream, the framework runs the tool, and only then does anything else come down the wire. So
    "an update went by without adding to it" fired after execution, and the trace announced
    `predict_pka(...)` once the twenty seconds were already spent. From the chemist's side a
    working calculation and a hung server were the same thing.

    So a call now completes the moment its accumulated arguments **parse as JSON**, which is
    exactly when the provider has finished sending them and before the tool is invoked. No new
    provider signal is needed and D-138's promise is kept: the event still carries the whole
    argument preview, it just no longer waits for the result to prove the arguments ended. The
    update-went-by rule stays as the fallback for arguments that never parse (a provider that
    streams something other than JSON) so nothing can be stranded.

    Results are matched back by `call_id` and emitted as `ToolResultEvent`, which is why
    `_names` outlives the flush: the name is what the result event reports, and it is not on the
    result content.

    Still duck-typed: MAF's function-call content class is not a stable top-level export and its
    shape varies by version, so this matches on structure (a `call_id`/`arguments` pair) rather
    than importing a concrete type.
    """

    def __init__(self) -> None:
        """Start an empty trace; one per turn, since every field is scoped to that turn."""
        self._names: dict[str, str] = {}
        self._fragments: dict[str, list[str]] = {}
        # The name of every call already announced, kept so its result can be reported under the
        # same name. Bounded by the calls in one turn, which the loop cap already bounds.
        self._issued: dict[str, str] = {}
        # What this turn's tools returned, in full, in the order they came back — the evidence the
        # answer verifier and the parameter-shape gate check the answer against. Kept here rather
        # than read back off the emitted `ToolResultEvent`s because those carry a 200-character
        # *preview*: a `gather_evidence` result is ~20,000 characters over 40 chunks, so scoring
        # against the preview would call 39 of its 40 citations fabricated. The budget is right for
        # the UI and wrong for a grounding check, so the two read different things from one place.
        # Bounded by the calls in one turn, like `_issued`, and never leaves the process.
        self.outputs: list[str] = []

    def feed(self, update: Any) -> list[Event]:
        """Take one streamed update; return the calls it issued and the results it returned."""
        growing: set[str] = set()
        done: set[str] = set()
        results: list[Event] = []
        for content in getattr(update, "contents", None) or []:
            if not (hasattr(content, "arguments") or hasattr(content, "call_id")):
                continue
            name = str(getattr(content, "name", "") or "")
            key = str(getattr(content, "call_id", "") or "") or name
            if not key:
                continue
            if name:
                self._names.setdefault(key, name)
            if key not in self._names and key not in self._issued:
                continue  # a fragment for a call whose opening content we never saw
            arguments = getattr(content, "arguments", None)
            if arguments is None:
                # The call id with no arguments field at all: this is the call's *result* coming
                # back, so it must not count as the call still growing. Note the test is `is
                # None` and not falsiness — an empty string is a real fragment of the argument
                # stream, and treating it as the end flushed the call before its arguments had
                # arrived, which is how this reached a second live run still empty.
                text = _result_text(content)
                if text is not None:
                    self.outputs.append(text)
                    tool = self._issued.get(key) or self._names.get(key, key)
                    results.append(ToolResultEvent(tool=tool, preview=text[:_ARG_PREVIEW_CHARS]))
                continue
            if name and arguments:
                # The name and the complete arguments in one content: the call arrived whole
                # rather than streamed, so it is finished now, and waiting would only delay it
                # behind the next update's text. The streamed shape never looks like this — its
                # named content carries empty arguments and its fragments carry no name.
                self._fragments[key] = [
                    json.dumps(arguments) if isinstance(arguments, Mapping) else str(arguments)
                ]
                done.add(key)
            else:
                fragments = self._fragments.setdefault(key, [])
                if isinstance(arguments, str) and arguments:
                    fragments.append(arguments)
                elif isinstance(arguments, Mapping) and arguments:
                    fragments[:] = [json.dumps(arguments)]
            growing.add(key)
            if _arguments_complete(self._fragments.get(key)):
                # The provider has finished sending this call's arguments, so the tool is about
                # to run. Announcing now is the whole point of D-159: waiting for the next update
                # means waiting for the result, and the wait between them is the part worth
                # showing.
                done.add(key)
        return [*self._take((set(self._fragments) - growing) | done), *results]

    def flush(self) -> list[Event]:
        """Emit whatever is still open — the stream ended before an untouched update arrived."""
        return self._take(set(self._fragments))

    def _take(self, keys: set[str]) -> list[Event]:
        events: list[Event] = []
        for key in [k for k in self._fragments if k in keys]:
            arguments = "".join(self._fragments.pop(key))[:_ARG_PREVIEW_CHARS]
            name = self._names.pop(key, key)
            # Remembered rather than discarded: the result content carries no name, so this is
            # what lets `ToolResultEvent` report which tool answered.
            self._issued[key] = name
            events.append(ToolCallEvent(tool=name, arguments=arguments))
        return events


def _arguments_complete(fragments: list[str] | None) -> bool:
    """Whether the accumulated argument fragments are a finished JSON value.

    This is the signal that replaces "wait for the next update" as the moment a call is announced
    (D-159). It works because the provider streams a tool call's arguments as one JSON document
    and does not invoke the tool until that document is closed — so a successful parse is exactly
    the boundary between "still arriving" and "about to run", and it is knowable from the bytes
    already in hand rather than from something that follows.

    False for anything that does not parse, which keeps the old update-went-by rule as the
    fallback: a provider streaming a non-JSON argument format still gets its call announced, just
    at the previous, later moment. Nothing is stranded, and nothing regresses.
    """
    if not fragments:
        return False
    try:
        json.loads("".join(fragments))
    except (ValueError, TypeError):
        return False
    return True


def _result_text(content: Any, /) -> str | None:
    """What a function-result content returned, in full, or None when it carries nothing.

    Duck-typed over the attribute the framework version happens to use, for the same reason the
    call side is: the concrete content class is not a stable export. A result that is empty or
    unreadable yields None rather than a value the trace does not have — the trace should not claim
    one, and the verifier must not treat "nothing came back" as evidence.

    Untruncated on purpose. The caller truncates for the wire (`_ARG_PREVIEW_CHARS`, the UI's
    budget) and keeps the whole text for grounding, which are different jobs with different right
    answers; returning the preview here would silently make the second one impossible.

    Failures are deliberately not reported here. A raised call already surfaces as
    `ToolFailedEvent` through the tool middleware, which has the exception and its message; a
    second event for the same outcome would leave a consumer choosing which to believe.
    """
    for attribute in ("result", "output", "value", "text"):
        value = getattr(content, attribute, None)
        if value is None or value == "":
            continue
        return value if isinstance(value, str) else str(value)
    return None


def approval_prompt(request: Any) -> str:
    """Render a user-input/approval request as a short prompt string for the UI."""
    for attr in ("prompt", "message", "text", "description"):
        value = getattr(request, attr, None)
        if value:
            return str(value)
    return "Approval requested."
