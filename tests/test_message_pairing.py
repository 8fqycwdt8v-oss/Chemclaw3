"""The stored-history invariant: no tool call without its result (offline).

Deleting one half of a `tool_use`/`tool_result` pair leaves a thread the model rejects outright,
and nothing repairs it — the read-time repair that used to heal one direction went with the MAF
thread that needed it. So the rule is enforced where rows are *deleted*, and these pin the pure
form of it; `test_retention.py` pins the sweep that applies it.
"""

import ast
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, message_to_dict

import chemclaw
from chemclaw.agent.message_migration import LANGCHAIN_SHAPE, MAF_SHAPE
from chemclaw.agent.message_pairing import (
    droppable_rows,
    stored_call_ids,
    unmatched_call_ids,
    unmatched_result_ids,
    unreadable_rows,
)
from tests.legacy_rows import legacy_call, legacy_result, legacy_text


def _calls(*call_ids: str) -> AIMessage:
    """An assistant message carrying one tool call per id."""
    return AIMessage(
        content="",
        tool_calls=[{"name": "screen_hazards", "args": {}, "id": call_id} for call_id in call_ids],
    )


def _answer(call_id: str, result: str = "ok") -> ToolMessage:
    """The tool message answering `call_id`."""
    return ToolMessage(content=result, tool_call_id=call_id)


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
    stranded_result = [_answer("c1")]
    assert unmatched_result_ids(stranded_result) == {"c1"}
    assert unmatched_call_ids(stranded_result) == set()  # the mirror does not see it, by design

    stranded_call = [_calls("c2")]
    assert unmatched_call_ids(stranded_call) == {"c2"}
    assert unmatched_result_ids(stranded_call) == set()


# --- D-145: disposing of a row means disposing of the rows it is paired with -------------------


def test_neither_half_of_a_pair_may_be_dropped_alone() -> None:
    """The core guarantee: a call and its result survive or die together."""
    rows = [(1, frozenset[str]()), (2, frozenset({"c1"})), (3, frozenset({"c1"}))]
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
    rows = [(2, frozenset({"c1", "c2"})), (3, frozenset({"c1"})), (4, frozenset({"c2"}))]
    assert droppable_rows(rows, {2, 3}) == set()
    assert droppable_rows(rows, {2, 3, 4}) == {2, 3, 4}


def test_the_closure_is_order_independent() -> None:
    """A result stored before its call is still one component.

    A provider's own grouping is positional; storage order is not guaranteed to match it once
    retention has removed an interleaving row. `unmatched_call_ids` is already order-independent
    for the same reason, and the closure has to match it or the two would disagree about a group.
    """
    rows = [(1, frozenset({"c1"})), (2, frozenset({"c1"}))]
    assert droppable_rows(rows, {1}) == set()
    assert droppable_rows(rows, {1, 2}) == {1, 2}


def test_the_closure_contracts_rather_than_expanding() -> None:
    """The safety direction: never return a row the caller did not ask to delete.

    Expanding would let an age cutoff reach *forward* and delete a live tool result from a recent
    turn. Contracting can only return a subset, so the worst case is a straddling group surviving
    one more pass — harmless, and self-correcting once the partner also ages out.
    """
    rows = [(1, frozenset({"c1"})), (2, frozenset({"c1"}))]
    assert droppable_rows(rows, {1}) <= {1}
    assert droppable_rows(rows, set()) == set()


# --- reading the ids out of a stored row, in either shape --------------------------------------


def test_a_legacy_row_is_read_by_the_shape_maf_actually_wrote() -> None:
    """The MAF discriminators still decide which *stored* rows a sweep may delete.

    Every `session_messages` row written before the M6 conversion is a `Message.to_dict()`, and the
    conversion pass is resumable — so the sweep reads them whether or not it has run. A rename in
    those two strings would not raise: it would change what a nightly sweep destroys, silently.

    The payloads are frozen literals (`tests/legacy_rows.py`), captured from the real constructors
    and verified byte-for-byte against them. They used to be *built* through MAF, which was right
    while it was installed — the assertion is about what MAF wrote, so hand-writing the payload
    would only prove this file agrees with itself. That inverts once the dependency is gone: these
    bytes are historical data a production table still holds, and a fixture that needs the library
    re-installed to exist is a fixture that cannot outlive it.
    """
    assert stored_call_ids(legacy_call("c1", "t")) == frozenset({"c1"})
    assert stored_call_ids(legacy_result("c1")) == frozenset({"c1"})
    assert stored_call_ids(legacy_text("user", "hi")) == frozenset()


def test_a_converted_row_is_read_by_the_shape_langchain_writes() -> None:
    """The other half of the same obligation: a row the conversion has already rewritten.

    Both shapes live in one table during a rollout, which is the whole reason `message_shape`
    exists. The previous reader used MAF's `Message.from_dict` for every row and **raised**
    `TypeError` on one of these — so the sweep crashed on any session that had taken a turn since
    the conversion, Temporal retried it to exhaustion, and retention silently stopped for exactly
    the sessions still in use.
    """
    call = message_to_dict(_calls("c1"))
    assert stored_call_ids(call) == frozenset({"c1"})
    assert stored_call_ids(message_to_dict(_answer("c1"))) == frozenset({"c1"})
    assert stored_call_ids(message_to_dict(HumanMessage(content="hi"))) == frozenset()


def test_a_row_in_neither_shape_is_unreadable_rather_than_pairing_free() -> None:
    """`None`, not an empty set — and the difference is what stops a sweep stranding a partner.

    Empty means "in no pairing, so disposable on its own". An unreadable row is not that: nothing
    can be concluded about what it is paired with. Collapsing the two would make it look
    pairing-free and therefore droppable, which is the one direction this module exists to prevent.
    """
    assert stored_call_ids({"something": "else"}) is None
    assert stored_call_ids({"contents": "not a list"}) is None


def test_an_unreadable_row_takes_its_whole_session_out_of_the_sweep() -> None:
    """Refusing the row is not enough, and that is the subtle half.

    An unreadable row links to nothing, so leaving it merely undroppable would let a partner it
    *would* have protected stay eligible — the sweep then strands exactly the pairing this rule
    exists to protect, reached by being careful about the wrong row. So the session is refused
    whole, and the caller is told which rows to look at.
    """
    rows = [(1, frozenset({"c1"})), (2, None), (3, frozenset({"c1"}))]
    assert unreadable_rows(rows) == [2]
    assert droppable_rows(rows, {1, 3}) == set(), "a session with an unreadable row was pruned"


def test_each_stored_shape_stamp_is_defined_exactly_once_in_the_tree() -> None:
    """Two modules read the stamp; only one may *say* what it is.

    `message_pairing` carried its own `_LANGCHAIN_SHAPE = "langchain"` under a comment claiming it
    was "named from that module so the two cannot drift" — but taking a *name* from a module is not
    importing its *value*, and two independent literals that happen to agree are two literals that
    can stop agreeing. The direction that matters is destructive: `stored_call_ids` decides what
    `droppable_rows` may delete, so a stamp the migration writes and this module does not recognise
    turns a protected pairing into a droppable row, silently.

    Written as a uniqueness scan over the package rather than as an equality assertion between the
    two names, because equal string literals are interned — `is` would pass on the copy this test
    exists to reject. It also catches the *next* copy, wherever it is made.
    """
    stamps = {MAF_SHAPE, LANGCHAIN_SHAPE}
    package = Path(chemclaw.__file__).parent
    definitions: dict[str, list[str]] = {stamp: [] for stamp in stamps}
    for path in sorted(package.rglob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AnnAssign)
                else []
            )
            value = getattr(node, "value", None)
            if not targets or not isinstance(value, ast.Constant) or value.value not in stamps:
                continue
            definitions[value.value].append(str(path.relative_to(package)))

    assert definitions == {
        MAF_SHAPE: ["agent/message_migration.py"],
        LANGCHAIN_SHAPE: ["agent/message_migration.py"],
    }, f"a stored-shape stamp is defined in more than one place: {definitions}"
