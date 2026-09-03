"""What a tool result *is*, given that one tool does not return a `ToolMessage`.

Every `wrap_tool_call` middleware in this repository that rewrites what the model reads was written
against one shape — `ToolMessage` in, `ToolMessage` out — and guarded itself with
`if not isinstance(result, ToolMessage): return result`. That guard is correct and it is not
complete: **`task` returns a `langgraph.types.Command`**, because a spawned helper has to write its
report *and* the channels that cross the subagent boundary (`model_calls`, `billed_tokens`, the
helper's `files`) into the caller's state in one act. Measured on the compiled graph, the object
that reaches the tool middleware chain for `task` is
`Command(update={'files': …, 'model_calls': …, 'messages': [ToolMessage(…)]})`.

So the guard silently excused the one tool whose result is **unbounded prose a model wrote**, and
two controls whose own docstrings say they apply to every tool did not apply to it
(`D-2026-08-29-a-helpers-report-is-model-prose-in-its-callers-thread`):

- `agent/tool_framing.py` left a helper's report undefanged, so a report reproducing the envelope
  delimiter — which a helper can *copy* rather than guess, having just read it around its own
  evidence — reached the caller's thread with a live one.
- `agent/tool_result_size.py` did not bound it. Upstream's `FilesystemMiddleware` evicts a result
  over `tool_token_limit_before_evict` (20,000 tokens × 4 chars = **80,000 chars**), and this
  repository's own ceiling is `agent_max_tool_result_chars` (**60,000**) — so a report measured at
  **70,048 characters** landed in the caller's thread whole, with neither control applied.

This module is the seam that fixes both in one place rather than two, which is the point: a third
middleware that rewrites a result will reach for the same function and inherit the same coverage,
where a second copy of the `isinstance` guard would inherit the same hole.
"""

import dataclasses
from collections.abc import Callable
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.types import Command


def rewritten_tool_messages(result: Any, rewrite: Callable[[ToolMessage], ToolMessage]) -> Any:
    """Apply `rewrite` to every `ToolMessage` in `result`, for the shapes a tool here returns.

    Three shapes, and the third is why this exists:

    - a bare `ToolMessage` — rewritten and returned, which is what every caller did before;
    - a `Command` carrying `update["messages"]` — each `ToolMessage` in that list is rewritten and
      the command is rebuilt with **every other key of the update preserved**, because those keys
      are how a helper's `model_calls` and `billed_tokens` reach the caller's channels. Dropping
      them would take a fan-out's spend off the one budget it shares, which is a defect the shape
      of `tests/test_state_channels.py`'s whole subject: a write the graph never sees;
    - anything else — returned untouched. A tool may return a plain string or a `Command` that
      only routes, and neither is a result to rewrite.

    **`Command.update` is typed `Any`, and only its dict form is rewritten here.** LangGraph's own
    `Command._update_as_tuples` also accepts a sequence of `(key, value)` pairs and an annotated
    object, and a `Command` in either of those forms passes through this function with **both**
    controls unapplied — which is the defect this module exists to close, one shape further out.
    That is deliberate rather than overlooked: upstream's `_create_task_tool` builds a dict, and so
    does upstream's own `FilesystemMiddleware._intercept_large_tool_result`, so handling a form
    nothing produces would be a branch no test could reach honestly. What makes it safe is that the
    assumption is *asserted* rather than believed — `tests/test_upstream_surface.py` fails if the
    `task` tool stops returning a dict-shaped update, naming this module as the one that breaks.

    **Rebuilt only when something changed.** `dataclasses.replace` on an unchanged command would
    return a new object every call for no reason, and identity is the cheapest way for a caller to
    say "nothing to do" — which is what `bound_tool_results` relies on to leave a result it did not
    truncate exactly as it found it.

    Args:
        result: Whatever the tool handler returned.
        rewrite: How to transform one `ToolMessage`. Must return a `ToolMessage`; returning the
            same object is how a rewrite declines to change anything.

    Returns:
        The same shape, with its tool messages rewritten.
    """
    if isinstance(result, ToolMessage):
        return rewrite(result)
    if not isinstance(result, Command) or not isinstance(result.update, dict):
        return result
    messages = result.update.get("messages")
    if not isinstance(messages, list):
        return result
    rewritten = [rewrite(m) if isinstance(m, ToolMessage) else m for m in messages]
    if all(new is old for new, old in zip(rewritten, messages, strict=True)):
        return result
    return dataclasses.replace(result, update={**result.update, "messages": rewritten})
