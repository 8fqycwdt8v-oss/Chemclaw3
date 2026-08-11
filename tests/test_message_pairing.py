"""The stored-history invariant: no tool call without its result (offline).

Deleting one half of a `tool_use`/`tool_result` pair leaves a thread the model rejects outright,
and nothing repairs it — the read-time repair that used to heal one direction went with the MAF
thread that needed it. So the rule is enforced where rows are *deleted*, and these pin the pure
form of it; `test_retention.py` pins the sweep that applies it.
"""

from agent_framework import Content, Message

from chemclaw.agent.message_pairing import (
    droppable_rows,
    unmatched_call_ids,
    unmatched_result_ids,
)


def _call(call_id: str, name: str = "screen_hazards") -> Content:
    return Content.from_function_call(call_id=call_id, name=name, arguments={})


def _result(call_id: str, result: str = "ok") -> Content:
    return Content.from_function_result(call_id=call_id, result=result)


def test_either_half_left_alone_is_reported_and_neither_is_repaired() -> None:
    """Both directions are asked, and both stop at reporting — the reason `droppable_rows` exists.

    The rest of this module answers "may I delete this?". These two answer "did somebody already
    delete the wrong thing?", and deliberately stop there: healing either half would destroy
    evidence and mask the bug that stranded it. That makes them a test's instruments rather than
    production calls, so this is the test that says out loud what they are for — and that the
    module offers no way to make either problem disappear quietly.

    The symmetry is worth pinning because it did not always hold. A read-time repair used to strip
    an unanswered call, so that direction self-healed and only the stranded result was permanent;
    the repair went with the MAF thread it served, and now nothing heals either.
    """
    stranded_result = [Message(role="tool", contents=[_result("c1")])]
    assert unmatched_result_ids(stranded_result) == {"c1"}
    assert unmatched_call_ids(stranded_result) == set()  # the mirror does not see it, by design

    stranded_call = [Message(role="assistant", contents=[_call("c2")])]
    assert unmatched_call_ids(stranded_call) == {"c2"}
    assert unmatched_result_ids(stranded_call) == set()


# --- D-145: disposing of a row means disposing of the rows it is paired with -------------------


def test_neither_half_of_a_pair_may_be_dropped_alone() -> None:
    """The core guarantee: a call and its result survive or die together."""
    rows = [
        (1, Message(role="user", contents=[Content.from_text("hi")])),
        (2, Message(role="assistant", contents=[_call("c1")])),
        (3, Message(role="tool", contents=[_result("c1")])),
    ]
    assert droppable_rows(rows, {2}) == set(), "the call was dropped without its result"
    assert droppable_rows(rows, {3}) == set(), "the result was dropped without its call"
    assert droppable_rows(rows, {2, 3}) == {2, 3}
    # A row that mentions no call_id is its own component and needs no partner.
    assert droppable_rows(rows, {1}) == {1}


def test_the_closure_is_transitive_across_parallel_calls() -> None:
    """One assistant message with two calls binds *both* result rows into one component.

    A single-pass "does this row's partner come along?" filter passes this case wrongly: row 2's
    partner row 3 is a candidate, so a naive check would drop {2, 3} and strand row 4's result.
    """
    rows = [
        (2, Message(role="assistant", contents=[_call("c1"), _call("c2")])),
        (3, Message(role="tool", contents=[_result("c1")])),
        (4, Message(role="tool", contents=[_result("c2")])),
    ]
    assert droppable_rows(rows, {2, 3}) == set()
    assert droppable_rows(rows, {2, 3, 4}) == {2, 3, 4}


def test_the_closure_is_order_independent() -> None:
    """A result stored before its call is still one component.

    MAF's own grouping is positional; storage order is not guaranteed to match it once compaction
    has removed an interleaving row. `unmatched_call_ids` is already order-independent for the same
    reason, and the closure has to match it or the two would disagree about what a group is.
    """
    rows = [
        (1, Message(role="tool", contents=[_result("c1")])),
        (2, Message(role="assistant", contents=[_call("c1")])),
    ]
    assert droppable_rows(rows, {1}) == set()
    assert droppable_rows(rows, {1, 2}) == {1, 2}


def test_the_closure_contracts_rather_than_expanding() -> None:
    """The safety direction: never return a row the caller did not ask to delete.

    Expanding would let an age cutoff reach *forward* and delete a live tool result from a recent
    turn. Contracting can only return a subset, so the worst case is a straddling group surviving
    one more pass — harmless, and self-correcting once the partner also ages out.
    """
    rows = [
        (1, Message(role="assistant", contents=[_call("c1")])),
        (2, Message(role="tool", contents=[_result("c1")])),
    ]
    assert droppable_rows(rows, {1}) <= {1}
    assert droppable_rows(rows, set()) == set()


def test_the_maf_discriminators_still_match_what_maf_emits() -> None:
    """The two strings that decide which rows a data-destroying sweep may delete.

    `durable/retention.py` prunes chat rows and asks this module which ones are safe to drop, and
    the answer turns on `content.type == "function_call"` / `"function_result"`. Those are MAF's
    strings, not ours. A rename upstream does not raise: it changes what a nightly sweep deletes.
    Measured against a plausible rename, `droppable_rows([(1, call), (2, result)], {1})` went from
    `set()` — the partner correctly protected — to `{1}`, deleting the call and stranding its
    result, which leaves a thread the API rejects and which nothing repairs.

    **This is not the guard, and the commit that added it overclaimed.** Mutating the two constants
    fails several tests in this file besides it — every pairing assertion turns on
    `content.type == _CALL`, so the coupling was already covered. What this adds is a *named*
    failure: the others report a pairing that was not spotted, which sends a reader to the pairing
    logic, while this one says the discriminator moved. Worth keeping for that reason and not worth
    claiming as a closed risk.
    """
    from chemclaw.agent.message_pairing import _CALL, _RESULT

    # Built through MAF's own public constructors, which is the point: the assertion is about what
    # MAF *emits*, so constructing the content any other way would only prove this file agrees with
    # itself. (`FunctionCallContent` is not exported at the top level in 1.11.0 — it lives behind
    # `agent_framework._types` — so the factory is also the only non-private route.)
    assert Content.from_function_call(call_id="c1", name="t", arguments={}).type == _CALL, (
        "MAF renamed the function-call discriminator; retention's droppable-row rule now "
        "mis-classifies every tool exchange"
    )
    assert Content.from_function_result(call_id="c1", result="ok").type == _RESULT, (
        "MAF renamed the function-result discriminator; retention can now strand a call's partner"
    )
