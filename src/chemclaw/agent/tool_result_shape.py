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
import logging
from collections.abc import Callable
from typing import Any

from deepagents.backends.utils import create_file_data
from langchain_core.messages import ToolMessage
from langgraph.types import Command

logger = logging.getLogger(__name__)


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
    That is deliberate rather than overlooked: upstream's `_build_task_tool` builds a dict, and so
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


#: Where the one entry naming a dropped set lands. A path rather than a per-file marker, because
#: the whole point is that the count is what had to be bounded: one notice for the set keeps the
#: total bounded, where a marker each is the 44N the cap exists to stop.
_DROPPED_PATH = "/scratch/_files_the_budget_could_not_hold.md"


def _dropped_head(count: int) -> str:
    """The part of the dropped-set notice that is a fact rather than a sample.

    Separated from the sample because it is what the caller has to *reserve* room for before it
    spends anything: the notice is itself an entry in the channel it is explaining, and a bound
    that forgets its own notice is the defect `bounded_content` fixed one level down by charging
    its notice against the limit rather than adding it on top.

    The only variable is the count, so its length grows monotonically with it — which is what lets
    a caller reserve against the number of files that *could* be dropped and be sure the notice for
    the number actually dropped fits.
    """
    return (
        f"[system] {count} file(s) a helper wrote were **not stored**: this caller's `files` "
        f"budget (`agent_subagent_files_max_chars`) cannot hold them even as truncation notices. "
        f"Reading one back will fail."
    )


def _dropped_notice(paths: list[str], budget: int) -> str:
    """The one entry that stands for every file the channel could not represent.

    **The sample is cut to fit `budget`, and it used to be cut to ten paths.** That bounded the
    count of the sample and not its length, and a path is not text this system wrote: it is the
    string a *model* passed to `write_file`. Measured through the shipped middleware at a
    200,000-character budget, ten dropped paths of 1,000 characters each put the stored total at
    201,517 — the notice being the unbounded thing this docstring's own previous version said it
    must not be.

    What it never drops is the count and the fact that reading one back will fail, because those
    are the two things a caller cannot act correctly without. The sample is the part that is nice
    to have, so the sample is the part that shrinks.
    """
    head = _dropped_head(len(paths))
    # Reserved at the *widest* form of the "and N more" clause — `len(paths)` bounds that N — for
    # the same reason `bounded_content` measures its notice at the widest form of its own numbers.
    used = len(head) + len(" Dropped: ") + len(f" and {len(paths)} more") + len(".")
    sample: list[str] = []
    for path in paths:
        step = len(path) + (2 if sample else 0)
        if used + step > budget:
            break
        sample.append(path)
        used += step
    if not sample:
        return head
    more = f" and {len(paths) - len(sample)} more" if len(sample) < len(paths) else ""
    return f"{head} Dropped: {', '.join(sample)}{more}."


def rewritten_command_files(
    result: Any,
    rewrite: Callable[[str, int], str],
    existing: Any = None,
    budget: int | None = None,
) -> Any:
    """Apply `rewrite` to every file a `Command` **changes** in its caller's state.

    **The other half of what `task` hands back, and nothing bounded it.**
    `rewritten_tool_messages` above covers the report — the part a model reads — and
    `D-2026-08-29-a-helpers-report-is-model-prose-in-its-callers-thread` established that the
    caller's *thread* stays tiny: driven, a helper reading 2 MB leaves its caller 57 characters.
    That measurement is right and it is about one of the two things a helper returns. Upstream's
    `_return_command_with_state_update` copies **every** non-excluded key of the helper's final
    state into the caller's update, and `files` is one of them — so the same probe puts
    **2,000,137 characters** of the helper's scratch filesystem into the caller's checkpointed
    state, where the thread shows 57.

    It is a *storage* blow-out rather than a context one, and the two need different arithmetic.
    The report is bounded against `agent_max_tool_result_chars` because it is sent to a model; a
    file is bounded against what a checkpoint costs, because LangGraph writes the whole channel
    per superstep and per version.

    **Changes, not writes — and the difference is the caller's own documents.** deepagents hands a
    subagent every non-excluded key of its caller's state and copies them all back
    (`_EXCLUDED_STATE_KEYS` is `messages`, `todos`, `structured_response`), so the `files` this
    `Command` carries is the caller's **whole** channel, not the helper's contribution to it.
    Cutting all of it charged a chemist's own `/scratch/` documents against a budget that bounds
    what a *helper* adds, and at an exhausted channel it destroyed them: measured, a chemist's
    200,000-character file came back as 45 characters because a helper had returned, with the
    truncation logged as "a file a helper wrote".

    **A cap on each file's size is not a cap on the command, because the cut has a floor.**
    `bounded_content` never returns less than the notice that says it cut — a bound paid for by
    saying nothing is not what this module is for — so N files each cut to that notice is 44N, and
    past a crossover the total grows linearly in N again. Driven before this loop spent a
    remainder: eight concurrent `task` calls of 600 changed files each landed 206,400 characters
    against a 200,000-character budget, and one call of 5,000 files landed 215,000. The per-file
    share had already floored, so dividing it further could not help.

    So the count is bounded too — by the budget running out rather than by a count derived from it.
    A file the remainder cannot pay for is **omitted** rather than stored empty, and one entry at
    `_DROPPED_PATH` names how many went and why. Omitting is the louder failure of the two: reading
    a dropped path back fails with "no such file", where an empty one hands a chemist a document
    that simply stops — the silent cut this module exists to prevent. One notice covers the whole
    dropped set, which is what keeps the total bounded rather than moving the problem.

    **A path is charged too, and for a while nothing charged it.** The budget's subject is the
    caller's `files` channel, and a channel is its keys as much as its values — LangGraph writes
    the mapping. `agent/tool_result_size._files_already_held` summed `content` and never read a
    key, this loop divided a budget that had never seen one, and the sweep that was supposed to
    hold the bound measured the same half. Driven through the shipped middleware at a
    200,000-character budget, one call of 5,000 changed files landed 191,517 characters of text
    under 83,370 characters of ordinary `/scratch/w0-4443.md` keys — **274,887 in the channel, 37%
    over, with no adversary at all** — and since a key is a string the *model* passed to
    `write_file`, 1,000-character paths took the same command to 4,728,887. So a file's share is
    reduced by its own key, and a key the remainder cannot pay for is what drops the file.

    Skipping them is not merely kinder, it is what the channel does anyway. Upstream's reducer is
    `result[key] = value`, so re-delivering a file whose text is unchanged is a no-op on the
    channel — the bound could only ever have cost bytes, never saved any. What is left to bound is
    exactly the set of paths whose text differs from what the caller already holds, and the loop
    divides the remainder over that set, so a helper that changed one file gets the whole budget
    instead of a share diluted by every document its caller happened to be carrying.

    Args:
        result: Whatever the tool handler returned.
        rewrite: Takes one file's text and how many characters this file may occupy, and returns
            the text to store. Returning the same string is how a rewrite declines to change
            anything — which the loop relies on, since identity is how it tells a cut from a pass.
        existing: The caller's `files` before this command lands. Files whose text it already holds
            unchanged are passed through untouched. `None` bounds every file, which is the old
            behaviour and is kept only for a caller that has no state to compare against.
        budget: How many characters this command may add to the caller's `files` channel, keys
            and text together. `None` bounds nothing and passes 0 as every share, which is how
            `agent_subagent_files_max_chars = 0` switches the cap off — `bounded_content` treats a
            non-positive limit as no cap, so the off switch has one spelling rather than two.

    Returns:
        The same shape, with its changed files rewritten.
    """
    if not isinstance(result, Command) or not isinstance(result.update, dict):
        return result
    files = result.update.get("files")
    if not isinstance(files, dict) or not files:
        return result
    # `FileData` is a mapping carrying `content` beside its timestamps, and treating it as one
    # rather than importing upstream's constructor is what keeps `created_at` intact — rebuilding
    # a file would restamp it. `tests/test_upstream_surface.py` asserts the shape, which is this
    # repository's discipline for every assumption about a library's data that the library does
    # not promise.
    held = existing if isinstance(existing, dict) else {}

    def _is_unchanged(path: str, content: str) -> bool:
        """Does the caller already hold this exact text at this path?"""
        before = held.get(path)
        return isinstance(before, dict) and before.get("content") == content

    changed_paths = [
        path
        for path, data in files.items()
        if isinstance(data, dict)
        and isinstance(data.get("content"), str)
        and not _is_unchanged(path, str(data["content"]))
    ]
    # Room for the one entry that names a dropped set, taken off the top before anything is spent.
    # `_dropped_head`'s only variable is the count and its length grows with it, so reserving
    # against every changed file is enough for the notice about however many actually went.
    reserve = 0 if budget is None else len(_DROPPED_PATH) + len(_dropped_head(len(changed_paths)))
    remaining = None if budget is None else max(budget - reserve, 0)
    left = len(changed_paths)
    rewritten: dict[str, Any] = {}
    changed = False
    dropped: list[str] = []
    for path, data in files.items():
        content = data.get("content") if isinstance(data, dict) else None
        if not isinstance(content, str) or _is_unchanged(path, content):
            rewritten[path] = data
            continue
        # The share is what is left divided by the files still to come, less this file's own key.
        # Dividing the *remainder* rather than the budget is what makes the bound exact: a file
        # that came in under its share hands what it did not spend to the ones after it, and a
        # file that floored has already been charged in full.
        share = 0 if remaining is None else max(remaining // max(left, 1) - len(path), 1)
        left -= 1
        bounded = rewrite(content, share)
        if remaining is not None and len(path) + len(bounded) > remaining:
            dropped.append(path)
            changed = True
            continue
        if remaining is not None:
            remaining -= len(path) + len(bounded)
        rewritten[path] = data if bounded is content else {**data, "content": bounded}
        changed = changed or bounded is not content
    if dropped:
        room = reserve - len(_DROPPED_PATH) + (remaining or 0)
        rewritten[_DROPPED_PATH] = create_file_data(_dropped_notice(dropped, room))
        logger.warning(
            "dropped %d file(s) a helper wrote: the caller's `files` budget cannot represent them "
            "even as truncation notices, so %s names the set rather than storing each one empty",
            len(dropped),
            _DROPPED_PATH,
        )
    if not changed:
        return result
    return dataclasses.replace(result, update={**result.update, "files": rewritten})
