"""Translating a compaction result into row deletions, without splitting a pairing (D-151).

`plan_compaction` sits between two things that disagree: a MAF strategy that *annotates and
inserts*, and a table that *deletes and rewrites*. These pin the three places the translation can go
wrong — position vs identity, an inserted summary with no row, and annotations leaking into storage
— plus the one that matters most, that a deleted tool group always goes whole.

Pure: the real strategy, no database. That is deliberate, because these are the safety argument for
a feature whose failure mode is destroying a chemist's conversation, and an argument that skips
offline is not much of one.
"""

import asyncio
from collections.abc import Sequence

import pytest
from agent_framework import Content, Message
from agent_framework._compaction import (
    EXCLUDE_REASON_KEY,
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
)

from chemclaw.agent.chemclaw_agent import compaction_strategy
from chemclaw.agent.history_compaction import CompactionPlan, plan_compaction
from chemclaw.agent.message_pairing import unmatched_call_ids, unmatched_result_ids
from chemclaw.core.config import settings


def _turn(index: int) -> list[Message]:
    """One realistic turn: a question, a tool call, a fat result, an answer."""
    return [
        Message(role="user", contents=[Content.from_text(f"question {index}")]),
        Message(
            role="assistant",
            contents=[
                Content.from_function_call(call_id=f"c{index}", name="predict_pka", arguments={})
            ],
        ),
        Message(
            role="tool",
            contents=[
                Content.from_function_result(call_id=f"c{index}", result="payload " + "y" * 800)
            ],
        ),
        Message(role="assistant", contents=[Content.from_text(f"answer {index}")]),
    ]


def _rows(turns: int = 6) -> list[tuple[int, Message]]:
    """A session's rows, numbered as Postgres would number them."""
    return list(enumerate([m for i in range(turns) for m in _turn(i)], start=101))


def _plan(
    rows: Sequence[tuple[int, Message]], *, budget: int, protected: set[int] | None = None
) -> CompactionPlan:
    """Run the real strategy at `budget` and return its plan."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "agent_context_token_budget", budget)
    try:
        strategy, _ = compaction_strategy()
        return asyncio.run(plan_compaction(rows, strategy=strategy, protected=protected or set()))
    finally:
        monkeypatch.undo()


def test_a_generous_budget_plans_nothing() -> None:
    """Compaction is "reduce when applicable" — under budget, nothing is touched."""
    assert _plan(_rows(), budget=1_000_000).is_empty()


def test_a_deleted_tool_group_goes_whole() -> None:
    """The guarantee: what survives is always a sendable thread.

    Asserted on the *surviving* rows rather than on the plan's shape, because "no half of a pairing
    was left behind" is the property, and it is exactly what `unmatched_*` answer.
    """
    rows = _rows()
    plan = _plan(rows, budget=1500)
    assert plan.deletes, "nothing was planned for deletion at a budget that forces compaction"

    surviving = [
        next(m for row_id, m in plan.rewrites if row_id == rid)
        if any(row_id == rid for row_id, _ in plan.rewrites)
        else message
        for rid, message in rows
        if rid not in plan.deletes
    ]
    assert unmatched_call_ids(surviving) == set(), "a call was left without its result"
    assert unmatched_result_ids(surviving) == set(), "a result was left stranded — unrecoverable"


def test_a_collapsed_group_anchors_on_its_lowest_row() -> None:
    """The summary takes over the group's first row, so `ORDER BY id` still reads in order.

    This is what lets the whole feature avoid a schema change: `session_messages` cannot express
    "insert between rows 113 and 115", and never has to, because a summary only ever replaces a
    group that is being deleted anyway.
    """
    rows = _rows()
    plan = _plan(rows, budget=1500)
    assert plan.rewrites, "the strategy collapsed no tool group at this budget"
    for anchor, summary in plan.rewrites:
        assert anchor not in plan.deletes, "the anchor row was also deleted"
        assert (summary.text or "").startswith("[Tool results:")
        # The rows the summary stands in for are gone, and it sits where the first of them sat.
        assert anchor < max(plan.deletes)


def test_no_annotation_reaches_storage() -> None:
    """`_group`/`_excluded` must never be persisted.

    They round-trip through JSONB, and `annotate_message_groups` *trusts* an already-annotated
    prefix — so a persisted annotation would make the next pass group against stale spans. It also
    breaks `008_sessions.sql`'s promise that the column holds the MAF payload verbatim.
    """
    plan = _plan(_rows(), budget=1500)
    assert plan.rewrites
    for _anchor, summary in plan.rewrites:
        stored = summary.to_dict()
        properties = stored.get("additional_properties") or {}
        for key in (GROUP_ANNOTATION_KEY, EXCLUDED_KEY, EXCLUDE_REASON_KEY):
            assert key not in properties, f"{key} leaked into the stored payload"


def test_protected_rows_are_never_deleted() -> None:
    """The turn just written must survive, whatever the strategy says.

    The composed strategy's fallback can exclude *every* message when one payload is oversized. A
    turn that deleted the rows it had just stored would lose the conversation it was recording — so
    this is not a nicety, it is the difference between compaction and data loss.
    """
    rows = _rows()
    newest = {row_id for row_id, _ in rows[-4:]}
    plan = _plan(rows, budget=200, protected=newest)
    assert not (plan.deletes & newest), "compaction deleted the turn that had just been stored"


def test_an_inserted_summary_is_dropped_when_its_group_survives() -> None:
    """A summary earns a row only if the group it replaces is actually going.

    Otherwise the originals stay and a summary of them would duplicate history — the same content
    twice, which is worse than not compacting at all.
    """
    rows = _rows()
    # Protect everything: nothing may be deleted, so no summary may be anchored either.
    plan = _plan(rows, budget=1500, protected={row_id for row_id, _ in rows})
    assert plan.deletes == frozenset()
    assert plan.rewrites == ()
