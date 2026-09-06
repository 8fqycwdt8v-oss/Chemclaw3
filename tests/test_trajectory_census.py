"""The trajectory census computes exactly what its ADR defines.

D-2026-08-27-count-the-trajectories-before-building-the-distiller turned "how many recurring
trajectories are there" from a question someone would answer ad hoc into two pure functions, so
the definitions — a turn's sequence, a retry collapsed, recurrence across sessions, and the
would-have-helped ordering — are held here against constructed messages rather than trusted.
"""

from datetime import UTC, datetime

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chemclaw.cli.trajectory_census import (
    SessionFailures,
    Turn,
    census,
    failed_and_recovered,
    normalized_tools,
)


def _ai(*tools: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": t, "args": {}, "id": f"c-{i}-{t}"} for i, t in enumerate(tools)],
    )


def _at(day: int) -> datetime:
    return datetime(2026, 8, day, tzinfo=UTC)


def test_a_turn_is_the_tools_between_two_human_messages_with_retries_collapsed() -> None:
    """The ADR's definition of a trajectory, held: order kept, consecutive duplicates are one."""
    messages = [
        HumanMessage("what is the pKa?"),
        _ai("gather_evidence"),
        _ai("compute_xtb_energy", "compute_xtb_energy"),
        _ai("record_knowledge_note"),
        HumanMessage("thanks — now the solubility"),
        _ai("predict_solubility"),
    ]
    assert normalized_tools(messages) == [
        ("gather_evidence", "compute_xtb_energy", "record_knowledge_note"),
        ("predict_solubility",),
    ]


def test_a_tool_used_twice_non_consecutively_stays_two_steps() -> None:
    """Collapsing is for retries, not for a procedure that genuinely revisits a tool."""
    messages = [HumanMessage("q"), _ai("find_notes", "expand_note", "find_notes")]
    assert normalized_tools(messages) == [("find_notes", "expand_note", "find_notes")]


def test_an_empty_store_reports_zeros_rather_than_refusing() -> None:
    """Zeros, not a refusal: the empty corpus stays a number anyone can re-produce."""
    report = census([])
    assert report["turns"] == 0
    assert report["sessions"] == 0
    assert report["recurring_classes"] == []
    assert report["trigger"]["generator_greenlit"] is False


def test_recurrence_needs_two_sessions_not_two_turns() -> None:
    """A pattern seen many times in one session is episodic, not distillable — the ADR's line."""
    same_session = [
        Turn("s1", _at(1), ("gather_evidence", "predict_pka")),
        Turn("s1", _at(1), ("gather_evidence", "predict_pka")),
    ]
    assert census(same_session)["recurring_classes"] == []
    two_sessions = same_session + [Turn("s2", _at(2), ("gather_evidence", "predict_pka"))]
    classes = census(two_sessions)["recurring_classes"]
    assert len(classes) == 1
    assert classes[0]["sessions"] == 2


def test_would_have_helped_orders_sessions_by_their_timestamps() -> None:
    """The later session could have used a skill from the earlier one; same-time cannot."""
    later = census(
        [
            Turn("s1", _at(1), ("a", "b", "c")),
            Turn("s2", _at(5), ("a", "b", "c")),
        ]
    )["recurring_classes"][0]
    assert later["would_have_helped"] is True and later["multi_tool"] is True
    same_time = census(
        [
            Turn("s1", _at(1), ("a", "b")),
            Turn("s2", _at(1), ("a", "b")),
        ]
    )["recurring_classes"][0]
    assert same_time["would_have_helped"] is False


def test_missing_timestamps_do_not_inflate_the_greenlight() -> None:
    """An unknown order is not-helped — the number that greenlights a build must not guess."""
    report = census(
        [
            Turn("s1", None, ("a", "b", "c")),
            Turn("s2", None, ("a", "b", "c")),
        ]
    )
    assert report["recurring_classes"][0]["would_have_helped"] is False


def test_the_greenlight_is_the_adrs_stated_trigger() -> None:
    """>=5 classes across >=3 sessions with >=1 helped multi-tool class — and not one fewer."""
    turns = []
    # Five distinct recurring classes, each in two of three sessions, one of them length 3 with a
    # later-session repeat.
    for i in range(4):
        turns += [
            Turn("s1", _at(1), (f"t{i}", "x")),
            Turn("s2", _at(2), (f"t{i}", "x")),
        ]
    turns += [
        Turn("s1", _at(1), ("gather_evidence", "compute", "propose")),
        Turn("s3", _at(9), ("gather_evidence", "compute", "propose")),
    ]
    report = census(turns)
    assert report["trigger"]["generator_greenlit"] is True
    # Remove the helped multi-tool class and the light goes off.
    without = census(turns[:-2])
    assert without["trigger"]["generator_greenlit"] is False


def _call(tool: str, call_id: str) -> AIMessage:
    """One issued call, with the id its result will be joined back by."""
    return AIMessage(content="", tool_calls=[{"name": tool, "args": {}, "id": call_id}])


def _result(call_id: str, *, status: str = "success") -> ToolMessage:
    """A tool result, joined back to its call by id — the only place the tool's name comes from."""
    return ToolMessage(content="…", tool_call_id=call_id, status=status)


def test_a_failure_takes_its_name_from_the_call_not_from_the_result() -> None:
    """The join is the whole of the work: a `ToolMessage` carries an id, never the tool's name."""
    failed, recovered = failed_and_recovered(
        [HumanMessage("q"), _call("compute_xtb_energy", "c1"), _result("c1", status="error")]
    )
    assert failed == frozenset({"compute_xtb_energy"})
    assert recovered == frozenset()


def test_a_result_for_a_call_this_session_never_issued_is_ignored() -> None:
    """An unjoinable result would otherwise attribute a failure to the empty string."""
    failed, _ = failed_and_recovered([HumanMessage("q"), _result("orphan", status="error")])
    assert failed == frozenset()


def test_a_result_that_is_not_an_error_is_a_success() -> None:
    """Only `status == "error"` is a failure, matching `api/graph_stream.py`'s own test."""
    failed, _ = failed_and_recovered([HumanMessage("q"), _call("predict_pka", "c1"), _result("c1")])
    assert failed == frozenset()


def test_recovery_is_the_same_tool_working_later_in_the_same_session() -> None:
    """A later success on the tool that failed; a success *before* the failure is not one."""
    failed, recovered = failed_and_recovered(
        [
            HumanMessage("q"),
            _call("find_notes", "c1"),
            _result("c1", status="error"),
            _call("find_notes", "c2"),
            _result("c2"),
        ]
    )
    assert failed == frozenset({"find_notes"}) and recovered == frozenset({"find_notes"})

    _, before_only = failed_and_recovered(
        [
            HumanMessage("q"),
            _call("find_notes", "c1"),
            _result("c1"),
            _call("find_notes", "c2"),
            _result("c2", status="error"),
        ]
    )
    assert before_only == frozenset()


def test_a_failure_class_recurs_across_sessions_not_within_one() -> None:
    """The first arm's rule, applied to the second: one session's repeated trouble is episodic."""
    one = [SessionFailures("s1", _at(1), frozenset({"screen_hazards"}), frozenset())]
    assert census([], one)["failure_classes"] == []
    two = one + [SessionFailures("s2", _at(2), frozenset({"screen_hazards"}), frozenset())]
    classes = census([], two)["failure_classes"]
    assert len(classes) == 1 and classes[0]["sessions"] == 2


def test_a_repeat_counts_only_when_the_recovery_came_first() -> None:
    """The claim is that an earlier session already knew the answer, so its stamp must precede."""
    after = census(
        [],
        [
            SessionFailures(
                "s1", _at(1), frozenset({"gather_evidence"}), frozenset({"gather_evidence"})
            ),
            SessionFailures("s2", _at(5), frozenset({"gather_evidence"}), frozenset()),
        ],
    )["failure_classes"][0]
    assert after["repeated_after_recovery"] is True

    before = census(
        [],
        [
            SessionFailures(
                "s1", _at(5), frozenset({"gather_evidence"}), frozenset({"gather_evidence"})
            ),
            SessionFailures("s2", _at(1), frozenset({"gather_evidence"}), frozenset()),
        ],
    )["failure_classes"][0]
    assert before["repeated_after_recovery"] is False
    assert before["recovered_somewhere"] is True


def test_missing_timestamps_do_not_inflate_the_failure_greenlight() -> None:
    """Same rule as the first arm: an unknown order must not decide a build."""
    row = census(
        [],
        [
            SessionFailures("s1", None, frozenset({"t"}), frozenset({"t"})),
            SessionFailures("s2", None, frozenset({"t"}), frozenset()),
        ],
    )["failure_classes"][0]
    assert row["repeated_after_recovery"] is False


def test_the_failure_greenlight_is_the_adrs_stated_trigger() -> None:
    """>=3 classes across >=3 sessions with >=1 repeated after recovery — and not one fewer."""
    sessions = [
        SessionFailures("s1", _at(1), frozenset({"a", "b", "c"}), frozenset({"a"})),
        SessionFailures("s2", _at(5), frozenset({"a", "b"}), frozenset()),
        SessionFailures("s3", _at(6), frozenset({"c"}), frozenset()),
    ]
    trigger = census([], sessions)["trigger"]
    assert trigger["failure_classes"] == 3
    assert trigger["sessions_with_failure_recurrence"] == 3
    assert trigger["repeated_after_recovery_classes"] == 1
    assert trigger["failure_greenlit"] is True

    # Take the recovery away and the light goes off, with every other term unchanged.
    without = census(
        [], [SessionFailures("s1", _at(1), frozenset({"a", "b", "c"}), frozenset())] + sessions[1:]
    )["trigger"]
    assert without["failure_classes"] == 3
    assert without["failure_greenlit"] is False


def test_the_failure_arm_cannot_change_the_procedure_arms_verdict() -> None:
    """`generator_greenlit` still answers D-2026-08-27's question; `any_greenlit` is the union."""
    sessions = [
        SessionFailures("s1", _at(1), frozenset({"a", "b", "c"}), frozenset({"a"})),
        SessionFailures("s2", _at(5), frozenset({"a", "b"}), frozenset()),
        SessionFailures("s3", _at(6), frozenset({"c"}), frozenset()),
    ]
    trigger = census([], sessions)["trigger"]
    assert trigger["generator_greenlit"] is False
    assert trigger["failure_greenlit"] is True
    assert trigger["any_greenlit"] is True


def test_a_corpus_of_pure_repeated_failure_reports_zero_on_the_first_arm() -> None:
    """The blindness the second arm exists for, held as a fact rather than asserted in prose.

    Two sessions hit the same failure and take *different* routes around it, which is what a
    recurring failure actually looks like — so the sequences do not match and the procedure arm
    correctly reports nothing, while the failure arm sees the recurrence.
    """
    turns = [
        Turn("s1", _at(1), ("gather_evidence", "screen_hazards", "predict_pka")),
        Turn("s2", _at(5), ("screen_hazards", "find_notes")),
    ]
    sessions = [
        SessionFailures("s1", _at(1), frozenset({"screen_hazards"}), frozenset({"screen_hazards"})),
        SessionFailures("s2", _at(5), frozenset({"screen_hazards"}), frozenset()),
    ]
    report = census(turns, sessions)
    assert report["recurring_classes"] == []
    assert [c["tool"] for c in report["failure_classes"]] == ["screen_hazards"]
    assert report["failure_classes"][0]["repeated_after_recovery"] is True
