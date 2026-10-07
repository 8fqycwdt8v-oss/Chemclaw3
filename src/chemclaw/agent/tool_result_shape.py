"""What a tool result *is*, given that one tool does not return a `ToolMessage`.

Middlewares that rewrite what the model reads were written for `ToolMessage` in and out, but `task`
returns a `langgraph.types.Command` whose update carries the helper's report in `messages` alongside
the channels that cross the subagent boundary (`model_calls`, `billed_tokens`, `files`). An
`isinstance(result, ToolMessage)` guard would silently exempt the one tool whose result is unbounded
model-written prose from both defanging (`agent/tool_framing.py`) and bounding
(`agent/tool_result_size.py`). This module is the single seam both use, so a future rewriting
middleware inherits the coverage.
"""

import dataclasses
import itertools
import logging
from collections.abc import Callable
from typing import Any

from deepagents.backends.utils import create_file_data
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from chemclaw.agent.framing import SYSTEM_SPEECH_MARK
from chemclaw.core.model_prose import ModelProse

logger = logging.getLogger(__name__)


def rewritten_tool_messages(result: Any, rewrite: Callable[[ToolMessage], ToolMessage]) -> Any:
    """Apply `rewrite` to every `ToolMessage` in `result`, for the shapes a tool here returns.

    - a bare `ToolMessage`: rewritten and returned;
    - a `Command` with a dict `update["messages"]`: each `ToolMessage` is rewritten and every other
      update key is preserved, since those carry a helper's spend into the caller's channels;
    - anything else (a string, a routing-only `Command`): returned untouched.

    Only the dict form of `Command.update` is handled, because that is what upstream's `task` and
    `FilesystemMiddleware` produce; `tests/test_upstream_surface.py` fails if that changes. A
    command is rebuilt only when something changed, so identity tells a caller nothing was done.

    Args:
        result: Whatever the tool handler returned.
        rewrite: How to transform one `ToolMessage`. Must return a `ToolMessage`; returning the same
        object is how a rewrite declines to change anything.

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


# Where the one entry naming a dropped set lands: a single notice for the whole set keeps the total
# bounded, where a marker per file would not.
_DROPPED_PATH = "/scratch/_files_the_budget_could_not_hold.md"


def _notice_path(taken: Any) -> str:
    """`_DROPPED_PATH`, or the first free variant of it if something already holds that name.

    A fixed literal would silently overwrite a file the caller already holds there.
    """
    if not isinstance(taken, dict) or _DROPPED_PATH not in taken:
        return _DROPPED_PATH
    stem, _, suffix = _DROPPED_PATH.rpartition(".")
    names = (f"{stem}.{index}.{suffix}" for index in itertools.count(2))
    candidate = next(name for name in names if name not in taken)
    logger.warning(
        "%s is already held, so the notice naming what could not be stored went to %s",
        _DROPPED_PATH,
        candidate,
    )
    return candidate


def _dropped_head(count: int) -> str:
    """The part of the dropped-set notice that is a fact rather than a sample.

    Separate so the caller can reserve room for it before spending anything: the notice is itself an
    entry in the channel it explains. Its length grows monotonically with the count, so reserving
    for the worst case is enough. It says "this call's share" because the budget is the remaining
    allowance divided across sibling calls in the superstep
    (`agent/tool_result_size._files_budget`), not the whole budget.
    """
    return (
        f"[system] {count} file(s) a helper wrote were **not stored**: this call's share of the "
        f"`files` budget (`agent_subagent_files_max_chars`, divided across the calls in this step) "
        f"could not hold them even as truncation notices."
    )


def _reverted_head(count: int) -> str:
    """The same fact for a file the caller *already held*, where the outcome is the opposite.

    The channel reducer is `result[key] = value`, so omitting a key leaves the caller's previous
    text: a dropped edit reads back stale rather than failing. Such paths are served first, so this
    sentence is rare.
    """
    return (
        f"[system] {count} file(s) the helper edited were **left as this caller already had "
        f"them**: this call's share of the `files` budget could not hold the new text, so a read "
        f"returns the version from before the helper ran, not an error."
    )


def _dropped_notice(new: list[str], reverted: list[str], budget: int) -> str:
    """The one entry that stands for every file the channel could not represent.

    Two sentences because the two outcomes (missing vs stale) are opposite; see `_dropped_head` and
    `_reverted_head`. The path sample is cut to fit `budget` (paths are model-written and
    unbounded); the counts and the read consequences are never dropped.
    """
    heads = [
        head
        for head, paths in (
            (_dropped_head(len(new)), new),
            (_reverted_head(len(reverted)), reverted),
        )
        if paths
    ]
    head = " ".join(heads)
    paths = new + reverted
    # Reserved at the *widest* form of the "and N more" clause — `len(paths)` bounds that N — for
    # the same reason `bounded_content` measures its notice at the widest form of its own numbers.
    used = len(head) + len(" Affected: ") + len(f" and {len(paths)} more") + len(".")
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
    return f"{head} Affected: {', '.join(sample)}{more}."


def rewritten_command_files(
    result: Any,
    rewrite: Callable[[str, int], str],
    existing: Any = None,
    budget: int | None = None,
) -> Any:
    """Apply `rewrite` to every file a `Command` **changes** in its caller's state.

    The other half of what `task` hands back: upstream copies the helper's whole `files` channel
    into the caller's update, which can be very large even when the report is tiny. This bounds it
    against checkpoint cost rather than context.

    Only *changed* files are bounded: the command carries the caller's whole channel (deepagents
    excludes only `messages`, `todos` and `structured_response`), and re-delivering unchanged text
    is a no-op under the reducer, so the caller's own documents are never charged or truncated. The
    remainder is divided over the changed set.

    The count is bounded too, because each cut has a floor (the notice), so per-file shares alone
    cannot bound many files. A file the remainder cannot pay for is omitted rather than stored
    empty, and one entry at `_DROPPED_PATH` names what went. Keys are charged as well as text, since
    a channel is its keys too and paths are model-written.

    Args:
        result: Whatever the tool handler returned.
        rewrite: Takes one file's text and how many characters this file may occupy, and returns the
        text to store. Returning the same string declines to change anything; the loop uses identity
        to tell a cut from a pass.
        existing: The caller's `files` before this command lands; files it already holds unchanged
        pass through untouched. `None` bounds every file.
        budget: How many characters this command may add to the caller's `files` channel, keys and
        text together. `None` bounds nothing and passes 0 as every share (`bounded_content` treats a
        non-positive limit as no cap), which is how `agent_subagent_files_max_chars = 0` disables
        it.

    Returns:
        The same shape, with its changed files rewritten.
    """
    if not isinstance(result, Command) or not isinstance(result.update, dict):
        return result
    files = result.update.get("files")
    if not isinstance(files, dict) or not files:
        return result
    # `FileData` is treated as a mapping rather than rebuilt with upstream's constructor, which
    # keeps `created_at` intact; `tests/test_upstream_surface.py` asserts the shape.
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
    # Paths the caller already holds are served first, since omitting one leaves stale pre-edit text
    # rather than a clean failure. Stable sort, so the command's order is otherwise kept.
    ordered = sorted(changed_paths, key=lambda path: path not in held)
    # Reserve room for the dropped-set notice before spending anything; both heads grow
    # monotonically with their counts, so reserving against every changed file is enough.
    reserve = (
        0
        if budget is None
        else len(_DROPPED_PATH)
        + len(_dropped_head(len(changed_paths)))
        + len(_reverted_head(len(changed_paths)))
        + 1
    )
    remaining = None if budget is None else max(budget - reserve, 0)
    left = len(ordered)
    stored: dict[str, str] = {}
    dropped: list[str] = []
    reverted: list[str] = []
    for path in ordered:
        content = str(files[path]["content"])
        # The share is the remainder divided over the files still to come, less this file's own key;
        # a file under its share passes the surplus on, which keeps the bound exact.
        share = 0 if remaining is None else max(remaining // max(left, 1) - len(path), 1)
        left -= 1
        bounded = rewrite(content, share)
        if remaining is not None and len(path) + len(bounded) > remaining:
            (reverted if path in held else dropped).append(path)
            continue
        if remaining is not None:
            remaining -= len(path) + len(bounded)
        stored[path] = bounded
    rewritten: dict[str, Any] = {}
    changed = bool(dropped or reverted)
    for path, data in files.items():
        text = data.get("content") if isinstance(data, dict) else None
        if not isinstance(text, str) or _is_unchanged(path, text):
            rewritten[path] = data
            continue
        if path not in stored:
            continue
        kept = stored[path]
        rewritten[path] = data if kept is text else {**data, "content": kept}
        changed = changed or kept is not text
    if dropped or reverted:
        where = _notice_path(rewritten)
        room = reserve - len(where) + (remaining or 0)
        rewritten[where] = create_file_data(_dropped_notice(dropped, reverted, room))
        logger.warning(
            "could not store %d file(s) a helper wrote and %d it edited: this call's share of the "
            "`files` budget cannot represent them even as truncation notices, so %s names the set "
            "rather than storing each one empty",
            len(dropped),
            len(reverted),
            where,
        )
    if not changed:
        return result
    return dataclasses.replace(result, update={**result.update, "files": rewritten})


# The turn limits that can stop a helper, by the state flag its `Command` carries back and the name
# the caller's model is told. `loop_capped` and `spend_capped` cross the subagent boundary on
# purpose (`agent/state.py`).
_HELPER_STOPS: tuple[tuple[str, str], ...] = (
    ("loop_capped", "step limit"),
    ("spend_capped", "token budget"),
)

#: What a caller is told about a helper a turn limit stopped. `{limit}` is from `_HELPER_STOPS`.
HELPER_CUT_SHORT = ModelProse(
    "The helper was stopped by this turn's {limit} before it finished, so this is not a completed "
    "report. Partial findings — what it had written when it stopped, which may describe a next "
    "step it never took rather than a result — follow. Treat anything it did not report as "
    "unexamined, not as absent, and do not send another helper to repeat the same sweep: the "
    "limit that stopped this one is the turn's, and it is spent."
)


def helper_stopped_by(result: Any) -> str | None:
    """Which turn limit stopped the helper whose `task` result this is, or `None` if it finished.

    deepagents builds the report from the helper's last non-empty assistant text, so a capped
    helper's narration ("Let me also check…") would read as its findings. The flags its final state
    carries back say whether it finished.
    """
    if not isinstance(result, Command) or not isinstance(result.update, dict):
        return None
    for flag, limit in _HELPER_STOPS:
        if result.update.get(flag):
            return limit
    return None


def cut_short_report(report: str, limit: str) -> str:
    """A stopped helper's report, led by this system's marked statement that it is partial.

    Led rather than trailed, because the head is what a bounded cut keeps and what a reader sees
    first. Marked with `SYSTEM_SPEECH_MARK`, which the helper's own text cannot forge
    (`agent/tool_framing.py` defangs it out first).
    """
    findings = report.strip() or "(it had written nothing)"
    return f"[{HELPER_CUT_SHORT.format(limit=limit)}] {SYSTEM_SPEECH_MARK}\n\n{findings}"


#: What the model reads in place of a connector result that carried nothing at all.
EMPTY_TOOL_RESULT = ModelProse(
    "The tool ran and returned no content: no text, no data and no error. That is not the tool "
    "saying nothing was found — it said nothing either way, so do not report an absence on the "
    "strength of it. Say the tool returned nothing, and use another route if the answer matters."
)


def _is_blank(content: Any) -> bool:
    """Whether a `ToolMessage.content` carries nothing a model could read.

    Both `""` and `[]` occur (`langchain_mcp_adapters` returns `[]` for a result with zero content
    blocks). A non-text block (image, file) is content.
    """
    if isinstance(content, str):
        return not content.strip()
    if not isinstance(content, list):
        return False
    for block in content:
        if isinstance(block, str):
            text: Any = block
        elif isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
        else:
            return False
        if isinstance(text, str) and text.strip():
            return False
    return True


def returned_nothing(result: object) -> bool:
    """Whether a tool *succeeded* and handed back no content at all.

    "Found nothing" is a statement a tool makes; an empty result is a tool making none, and the two
    must not read the same to the model, the trace or the audit trail. A failure is not this
    (`returned_failure` owns `status="error"`). `isinstance`, so `ToolMessageChunk` is covered.
    """
    return (
        isinstance(result, ToolMessage) and result.status != "error" and _is_blank(result.content)
    )


def empty_result_notice() -> str:
    """The marked sentence the model reads for a connector result that carried nothing.

    Marked with `SYSTEM_SPEECH_MARK` and not framed: there is no evidence here, only this system's
    statement about the call.
    """
    return f"{EMPTY_TOOL_RESULT} {SYSTEM_SPEECH_MARK}"
