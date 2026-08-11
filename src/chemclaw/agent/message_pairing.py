"""The one rule a stored conversation must satisfy: every tool call carries its result.

Anthropic (and every other tool-calling API) rejects a thread in which a `tool_use` block is not
answered by a matching `tool_result` — "tool_use ids were found without tool_result blocks". A
thread that acquires an unmatched call is not one bad turn; it is a poison pill replayed on every
subsequent turn, so whatever holds it is bricked until somebody edits it.

**Nothing here heals such a thread any more, and that is deliberate.** This module used to carry a
read-time repair, because MAF's durable history *was* the thread sent to the model and a pod
eviction between the two writes left an orphan in it. The graph builds its thread from the
checkpointer now (D-2026-08-10 §2), so what is left are the two jobs that were never about healing:

- **A guard on deletion.** `droppable_rows` is the rule every code path that deletes conversation
  rows goes through, so an age cutoff or a compaction pass cannot take one half of a pair. It is
  used by `durable/retention.py` and `agent/history_compaction.py`, and it contracts rather than
  expands, so the worst case is a straddling group surviving one more sweep.
- **Assertions.** `unmatched_call_ids`, `unmatched_result_ids` and
  `calls_without_adjacent_results` are what a test uses to prove code that deletes or assembles
  messages did not strand anything. None has a production caller, and none should acquire one:
  their value is that they refuse to fix what they find, so a bug that strands a pairing fails a
  test instead of being cleaned up behind.

Pure functions over MAF `Message`s, with no I/O, so the storage layer, the retention sweep and the
tests can assert the same rule rather than each restating it.
"""

from collections.abc import Sequence
from collections.abc import Set as AbstractSet

from agent_framework import Message

# MAF's content-type discriminators. These two strings decide which rows `durable/retention.py`
# may delete, so an upstream rename does not fail loudly — it changes what a data-destroying
# nightly sweep does. Measured against a plausible rename: `droppable_rows([(1, call), (2, result)],
# {1})` went from `set()` (the partner correctly protected) to `{1}` — the sweep deletes the call
# row and strands its result, leaving a thread the API rejects outright. Silent: no exception, no
# failed activity, conversations that stop rendering days later and no longer say why.
#
# `test_message_pairing.py` asserts these still match what MAF emits, so a rename fails a test
# rather than a corpus. Pinning them as constants is not the guard — the test is; the constants are
# what gives the test one place to look.
_CALL = "function_call"
_RESULT = "function_result"


def unmatched_call_ids(messages: Sequence[Message]) -> set[str]:
    """Return the `call_id`s of function calls that no function result answers.

    Order-independent on purpose: a result is a valid answer to its call wherever it sits in the
    list, so this reports genuinely unanswered calls rather than merely out-of-order ones.

    One of the pair a deletion has to be checked against — see `unmatched_result_ids` for why both
    directions are asked and neither is repaired.
    """
    answered = {
        content.call_id
        for message in messages
        for content in message.contents
        if content.type == _RESULT and content.call_id is not None
    }
    # A call with no `call_id` is skipped rather than reported: nothing can be matched to it, so it
    # is also nothing this function could name. Real tool calls always carry one — it is what the
    # API pairs on.
    return {
        content.call_id
        for message in messages
        for content in message.contents
        if content.type == _CALL and content.call_id is not None and content.call_id not in answered
    }


def unmatched_result_ids(messages: Sequence[Message]) -> set[str]:
    """Return the `call_id`s of function *results* that no function call accounts for.

    The mirror of `unmatched_call_ids`, and both are deliberately assertions rather than repairs
    (D-145). There used to be an asymmetry here: a read-time repair stripped an unanswered *call*,
    so that direction self-healed while a stranded *result* did not. The repair is gone with the
    MAF thread that needed it, and the symmetry is the better state — stripping either half would
    silently destroy evidence and mask the bug in whatever produced it.

    Their job is to be what a test asks: any code that deletes conversation rows must *prove* it
    never leaves one of these, rather than rely on something cleaning up afterwards.
    `droppable_rows` is what makes that provable; these are what check it.
    """
    called = {
        content.call_id
        for message in messages
        for content in message.contents
        if content.type == _CALL and content.call_id is not None
    }
    return {
        content.call_id
        for message in messages
        for content in message.contents
        if content.type == _RESULT and content.call_id is not None and content.call_id not in called
    }


def droppable_rows(rows: Sequence[tuple[int, Message]], candidates: AbstractSet[int]) -> set[int]:
    """Narrow `candidates` to the rows that can be deleted without stranding a tool-call pairing.

    Storage may not dispose of a conversation row on its own terms: a `function_call` and the
    `function_result` answering it are one indivisible unit, and deleting either half alone bricks
    the session (`unmatched_result_ids` explains why the surviving half cannot be healed). Both
    callers that delete rows — durable compaction and age-based retention — choose their candidates
    for reasons that know nothing about pairing, so the pairing rule is applied once, here.

    Rows are joined into components by shared `call_id`, in **either** direction: a row is linked to
    every row mentioning an id it mentions, whether as the call or the result. The relation is
    transitive, which matters for parallel calls — one assistant message carrying three calls links
    to all three result rows, and those may link on again. A component survives or dies whole.

    **This contracts, it never expands.** A component with even one row outside `candidates` is
    dropped from the answer entirely, rather than pulling its remaining rows in. That direction is
    the safety property: expanding would let an age cutoff reach *forward* and delete a live result
    from a recent turn, whereas contracting can only ever return a subset of what the caller already
    chose — so the worst case is a straddling group surviving one more pass, which is harmless and
    self-correcting.

    Args:
        rows: Every row of the session, as `(row_id, message)` — not just the candidates. A
            candidate's partner is frequently *not* a candidate (that is precisely the case worth
            catching), so a partial view would report a split component as droppable.
        candidates: The row ids the caller wants to delete.

    Returns:
        The subset of `candidates` that is safe to delete.
    """
    # Union-find over row ids, keyed by call_id. A dict of representatives is enough at this size
    # (one session's history), and path compression keeps the transitive case honest.
    parent: dict[int, int] = {row_id: row_id for row_id, _ in rows}

    def find(row_id: int) -> int:
        while parent[row_id] != row_id:
            parent[row_id] = parent[parent[row_id]]
            row_id = parent[row_id]
        return row_id

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    first_row_for_call: dict[str, int] = {}
    for row_id, message in rows:
        for content in message.contents:
            call_id = content.call_id if content.type in (_CALL, _RESULT) else None
            if call_id is None:
                continue
            seen = first_row_for_call.setdefault(call_id, row_id)
            union(seen, row_id)

    members: dict[int, set[int]] = {}
    for row_id, _ in rows:
        members.setdefault(find(row_id), set()).add(row_id)
    return {
        row_id for component in members.values() if component <= candidates for row_id in component
    }


def calls_without_adjacent_results(messages: Sequence[Message]) -> set[str]:
    """Return `call_id`s whose result is not in the *immediately following* message.

    The stricter, on-the-wire form of the rule: the API does not merely require that a result
    exists somewhere, it requires it in the very next block — "tool_use ids were found without
    tool_result blocks **immediately after**". The two checks differ exactly where duplicated
    history does: replaying a call the transcript already answered leaves a second, unanswered copy
    that an exists-somewhere check still considers satisfied, because the id does appear answered
    once.

    Use this to validate what is about to be sent. It is deliberately *not* the rule storage goes
    by — there a merely out-of-order pair is intact history and must not be deleted, which is what
    `droppable_rows` enforces.
    """
    missing: set[str] = set()
    for index, message in enumerate(messages):
        called = {c.call_id for c in message.contents if c.type == _CALL and c.call_id is not None}
        if not called:
            continue
        following = messages[index + 1] if index + 1 < len(messages) else None
        answered = (
            {c.call_id for c in following.contents if c.type == _RESULT}
            if following is not None
            else set()
        )
        missing |= called - answered
    return missing
