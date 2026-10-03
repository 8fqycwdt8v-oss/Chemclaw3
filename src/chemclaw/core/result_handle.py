"""The handle a model reads at the foot of a tool result, and how every reader of numbers skips it.

**What it is.** Every tool result the model receives ends with one line, `⟨r:<12 hex>⟩` — the first
twelve hex digits of the `tool_result_blobs` content hash of that result's *full* text
(`agent/tool_result_size.bound_tool_results` stores it; `agent/tool_framing.stamp_result_handles`
appends the line). It is how an artefact binds a value to the result it came from
(`D-2026-10-03-an-artefact-binds-a-value-to-the-result-it-came-from`): the model writes
`{"$bind": {"result": "r:<hex>", "pointer": "/…"}}` and the server resolves the prefix inside the
session's own links.

**Why it lives in `core`.** Three layers read it and none may import another for it: the agent
stamps it, the API strips it before a result becomes a trace event, and `core.quantities` — the one
number grammar every grounding check shares — must never read its hex as a figure. Twelve hex
digits are all decimal digits about one time in three hundred (`(10/16)**12`), and a handle such as
`⟨r:123456789012⟩` would otherwise ground a twelve-digit figure nobody computed.
"""

import re

from chemclaw.core.config import settings

#: How many hex digits of the content hash the line carries — 48 bits, so two results of one
#: session sharing a prefix is a birthday bound over ~16 million results, and a collision is refused
#: by name rather than resolved by guess (`exhibits.bindings`).
HANDLE_HEX = 12

#: The one line a stamped result ends with: a newline, then the handle in mathematical angle
#: brackets — characters no tool result's JSON or Markdown uses as syntax, so the line cannot be
#: mistaken for part of the payload, and a reader can find it by shape.
_LINE = re.compile(r"\n⟨r:[0-9a-f]{" + str(HANDLE_HEX) + r"}⟩\Z")

#: A handle anywhere in text, bracketed or bare (`r:3fa2b1c0d9e8`, as a model quotes one in prose or
#: in a `$bind`), eight to sixty-four hex digits. Word-bounded so `vr:12345678` or a longer hex run
#: is not cut in half.
_ANYWHERE = re.compile(r"⟨?(?<![\w])r:[0-9a-f]{8,64}(?![\w])⟩?")


def handles_resolve() -> bool:
    """Whether a binding can resolve a handle on this deployment — and so whether one is stamped.

    One predicate for the stamp and the resolver (`agent/tool_framing.stamp_result_handles`,
    `exhibits.bindings`, `exhibits.grounding`). They disagreed: with the in-memory session store
    the result store still kept every result in Postgres, so the stamp put a handle on each one,
    and every binding the model then wrote was refused as "this deployment keeps no tool results" —
    an address handed out that nothing accepts. The session store is in the condition because the
    exhibit store follows it, and an artefact on the in-memory one has no durable session whose
    stored results a later read can scope to.
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

    What a surface and a grounding check read: the handle is an address the model was handed, not
    something the tool returned, so a trace event's preview, numbers and values are taken from the
    text without it.
    """
    return _LINE.sub("", text)


def without_handles(text: str) -> str:
    """`text` with every handle — bracketed or bare, anywhere — replaced by a space.

    For the number grammar (`core.quantities`): a handle quoted in an answer or left at a result's
    foot is an address, never a figure.
    """
    return _ANYWHERE.sub(" ", text)
