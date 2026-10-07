"""The handle a model reads at the foot of a tool result, and how every reader of numbers skips it.

Every tool result ends with `⟨r:<12 hex>⟩`, the first twelve hex digits of the `tool_result_blobs`
content hash of the full result (`agent/tool_result_size.bound_tool_results` stores it;
`agent/tool_framing.stamp_result_handles` appends it). An artefact binds a value to its source
result with `{"$bind": {"result": "r:<hex>", "pointer": "/…"}}`, resolved within the session.

In `core` because the agent stamps it, the API strips it, and `core.quantities` must never read its
hex as a figure (it is all digits often enough to ground a number nobody computed).
"""

import re

from chemclaw.core.config import settings

#: Hex digits of the content hash the line carries (48 bits); a collision within a session is
#: refused by name rather than guessed (`exhibits.bindings`).
HANDLE_HEX = 12

#: The one line a stamped result ends with: a newline, then the handle in mathematical angle
#: brackets, which no JSON or Markdown payload uses as syntax.
_LINE = re.compile(r"\n⟨r:[0-9a-f]{" + str(HANDLE_HEX) + r"}⟩\Z")

#: A handle anywhere in text, bracketed or bare (as a model quotes it), 8 to 64 hex digits,
#: word-bounded so a longer token is not cut in half.
_ANYWHERE = re.compile(r"⟨?(?<![\w])r:[0-9a-f]{8,64}(?![\w])⟩?")


def handles_resolve() -> bool:
    """Whether a binding can resolve a handle on this deployment — and so whether one is stamped.

    One predicate for the stamp and the resolvers (`agent/tool_framing.stamp_result_handles`,
    `exhibits.bindings`, `exhibits.grounding`), so no handle is handed out that nothing accepts. The
    session store is part of the condition because the exhibit store follows it, and an in-memory
    session has no durable results to scope a later read to.
    """
    return settings.session_store == "postgres" and settings.stream_max_result_bytes > 0


def handle_of(ref: str) -> str:
    """The handle a 64-hex content hash is shown to the model as: `r:` and its first 12 digits."""
    return f"r:{ref[:HANDLE_HEX]}"


def handle_line(ref: str) -> str:
    """The line appended to a stamped result: a newline and the bracketed handle."""
    return f"\n⟨{handle_of(ref)}⟩"


def without_handle_line(text: str) -> str:
    """`text` with its trailing handle line removed, or unchanged when it carries none.

    The handle is an address the model was handed, not tool output, so previews, numbers and values
    are read from the text without it.
    """
    return _LINE.sub("", text)


def without_handles(text: str) -> str:
    """`text` with every handle — bracketed or bare, anywhere — replaced by a space.

    For the number grammar: a handle is an address, never a figure.
    """
    return _ANYWHERE.sub(" ", text)
