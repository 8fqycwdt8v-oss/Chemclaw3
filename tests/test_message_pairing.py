"""The stored-history invariant: no tool call without its result.

Deleting half of a `tool_use`/`tool_result` pair leaves a thread the model rejects, and nothing
repairs it, so the rule is enforced where rows are deleted. These pin the pure form;
`test_retention.py` pins the sweep that applies it.
"""

import ast
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, message_to_dict

import chemclaw
from chemclaw.agent.message_migration import LANGCHAIN_SHAPE, MAF_SHAPE
from chemclaw.agent.message_pairing import (
    calls_without_adjacent_results,
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
    """Either half left alone is reported, and neither is repaired.

    These checks answer "did somebody already delete the wrong thing?" and stop at reporting:
    healing would destroy evidence and mask the bug. They are a test's instruments, not production
    calls.
    """
    stranded_result = [_answer("c1")]
    assert unmatched_result_ids(stranded_result) == {"c1"}
    assert unmatched_call_ids(stranded_result) == set()  # the mirror does not see it, by design

    stranded_call = [_calls("c2")]
    assert unmatched_call_ids(stranded_call) == {"c2"}
    assert unmatched_result_ids(stranded_call) == set()


# --- disposing of a row means disposing of the rows it is paired with -------------------------


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

    Storage order need not match the provider's positional grouping once retention removes a row;
    `unmatched_call_ids` is order-independent, and the closure must match it.
    """
    rows = [(1, frozenset({"c1"})), (2, frozenset({"c1"}))]
    assert droppable_rows(rows, {1}) == set()
    assert droppable_rows(rows, {1, 2}) == {1, 2}


def test_the_closure_contracts_rather_than_expanding() -> None:
    """The closure contracts: it never returns a row the caller did not ask to delete.

    Expanding could reach forward and delete a live result from a recent turn; contracting at worst
    keeps a straddling group one more pass.
    """
    rows = [(1, frozenset({"c1"})), (2, frozenset({"c1"}))]
    assert droppable_rows(rows, {1}) <= {1}
    assert droppable_rows(rows, set()) == set()


def test_a_row_mentioning_no_call_id_is_its_own_component() -> None:
    """A row mentioning no call id is its own component.

    Every row seeds the structure before joining; seeding only from joins would leave plain
    conversation rows in no component, so retention would reclaim only tool traffic.
    """
    rows = [(1, frozenset()), (2, frozenset({"c1"})), (3, frozenset({"c1"}))]
    assert droppable_rows(rows, {1}) == {1}
    assert droppable_rows(rows, {1, 2}) == {1}
    assert droppable_rows(rows, {1, 2, 3}) == {1, 2, 3}


def test_the_closure_joins_across_more_than_one_hop() -> None:
    """Four rows chained by three ids are one component, not three pairs.

    A path rather than a star, fed in an order that joins the far end first, so one round of joining
    is not enough.
    """
    rows = [
        (4, frozenset({"c3"})),
        (3, frozenset({"c2", "c3"})),
        (2, frozenset({"c1", "c2"})),
        (1, frozenset({"c1"})),
    ]
    for short in ({1}, {1, 2}, {1, 2, 3}, {2, 3, 4}):
        assert droppable_rows(rows, short) == set(), short
    assert droppable_rows(rows, {1, 2, 3, 4}) == {1, 2, 3, 4}


# --- reading the ids out of a stored row, in either shape --------------------------------------


def test_a_legacy_row_is_read_by_the_shape_maf_actually_wrote() -> None:
    """Legacy rows are read by the shape MAF actually wrote.

    Rows written before the conversion are `Message.to_dict()` output, and the pass is resumable, so
    the sweep reads them either way; a rename in the discriminators would silently change what is
    deleted. The payloads are frozen literals from `tests/legacy_rows.py`, historical data a table
    still holds.
    """
    assert stored_call_ids(legacy_call("c1", "t")) == frozenset({"c1"})
    assert stored_call_ids(legacy_result("c1")) == frozenset({"c1"})
    assert stored_call_ids(legacy_text("user", "hi")) == frozenset()


def test_a_converted_row_is_read_by_the_shape_langchain_writes() -> None:
    """A converted row is read by the shape LangChain writes.

    Both shapes coexist during a rollout; reading every row as MAF would raise on converted rows and
    stop retention for exactly the sessions in use.
    """
    call = message_to_dict(_calls("c1"))
    assert stored_call_ids(call) == frozenset({"c1"})
    assert stored_call_ids(message_to_dict(_answer("c1"))) == frozenset({"c1"})
    assert stored_call_ids(message_to_dict(HumanMessage(content="hi"))) == frozenset()


def test_a_row_in_neither_shape_is_unreadable_rather_than_pairing_free() -> None:
    """A row in neither shape is unreadable (`None`), not pairing-free (empty set).

    An empty set means disposable on its own; collapsing the two would make an unreadable row
    droppable and could strand its partner.
    """
    assert stored_call_ids({"something": "else"}) is None
    assert stored_call_ids({"contents": "not a list"}) is None


def test_a_payload_that_is_not_a_mapping_is_unreadable_rather_than_a_crash() -> None:
    """A payload that is not a mapping is unreadable rather than a crash.

    `message` is bare `jsonb`, and a raise here precedes the per-session skip, taking down the whole
    pass. `session_store.message_from_row` guards the same column the same way. For a string,
    `"contents" in payload` is a substring test, so such a row must not take the MAF branch.
    """
    for payload in ("contents of a corrupted row", "plain prose", [1, 2, 3], 42, True):
        assert stored_call_ids(payload, LANGCHAIN_SHAPE) is None, (  # type: ignore[arg-type]
            f"{payload!r} was not reported as unreadable"
        )
        assert stored_call_ids(payload) is None, (  # type: ignore[arg-type]
            f"{payload!r} was not reported as unreadable without a stamp"
        )


def test_an_unreadable_row_takes_its_whole_session_out_of_the_sweep() -> None:
    """An unreadable row takes its whole session out of the sweep.

    It links to nothing, so merely skipping it would leave a partner it protects eligible for
    deletion. The session is refused whole and the caller told which rows to inspect.
    """
    rows = [(1, frozenset({"c1"})), (2, None), (3, frozenset({"c1"}))]
    assert unreadable_rows(rows) == [2]
    assert droppable_rows(rows, {1, 3}) == set(), "a session with an unreadable row was pruned"


def test_a_complete_parallel_batch_is_not_flagged_as_unadjacent() -> None:
    """A complete parallel batch passes the adjacency rule; a split one does not.

    "Immediately after" refers to the following wire message, which holds every result of the batch:
    the answering window is the contiguous run of tool messages, not one slot.
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    batch = AIMessage(
        content="",
        tool_calls=[
            {"name": "a", "args": {}, "id": "c-1"},
            {"name": "b", "args": {}, "id": "c-2"},
            {"name": "c", "args": {}, "id": "c-3"},
        ],
    )
    thread = [
        HumanMessage("go"),
        batch,
        ToolMessage("r1", tool_call_id="c-1"),
        ToolMessage("r2", tool_call_id="c-2"),
        ToolMessage("r3", tool_call_id="c-3"),
        AIMessage("done"),
    ]
    assert calls_without_adjacent_results(thread) == set()

    # And the window is the *contiguous* run: a result parked past an intervening message is
    # exactly what the wire rejects, so it must still be flagged.
    broken = [
        HumanMessage("go"),
        batch,
        ToolMessage("r1", tool_call_id="c-1"),
        AIMessage("interlude"),
        ToolMessage("r2", tool_call_id="c-2"),
        ToolMessage("r3", tool_call_id="c-3"),
    ]
    assert calls_without_adjacent_results(broken) == {"c-2", "c-3"}


def test_every_unanswered_call_in_a_thread_is_reported_not_only_the_last() -> None:
    """Every unanswered call in a thread is reported, not only the last message's.

    `missing |= called - answered` must accumulate across assistant messages. This feeds
    `durable/retention.py`'s pre-delete check, so a hidden earlier half-pair would make the thread
    deletable.
    """
    from langchain_core.messages import AIMessage, HumanMessage

    def _asks(call_id: str) -> AIMessage:
        return AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": call_id}])

    thread = [
        HumanMessage("first"),
        _asks("c-early"),
        HumanMessage("second"),
        _asks("c-late"),
        HumanMessage("third"),
    ]

    assert calls_without_adjacent_results(thread) == {"c-early", "c-late"}


def test_a_legacy_row_whose_contents_hold_a_non_dict_is_read_rather_than_raising() -> None:
    """A legacy row whose `contents` holds a non-dict is read rather than raising.

    The `isinstance(item, dict)` guard keeps one bad element from raising before the per-session
    skip. The row is read, not skipped, so the call it does mention is still reported.
    """
    payload = {
        "contents": [
            "a bare string where a content block should be",
            {"type": "function_call", "call_id": "c9", "name": "t"},
        ]
    }

    assert stored_call_ids(payload) == frozenset({"c9"})


def test_each_stored_shape_stamp_is_defined_exactly_once_in_the_tree() -> None:
    """Each stored shape stamp is defined exactly once in the tree.

    `stored_call_ids` decides what `droppable_rows` may delete, so a second literal that drifted
    would make protected pairings droppable. A uniqueness scan over the package, since equal string
    literals are interned and an `is` check would pass on a copy; it also catches the next copy.
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
