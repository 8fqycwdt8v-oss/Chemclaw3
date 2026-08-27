"""The trajectory census computes exactly what its ADR defines.

D-2026-08-27-count-the-trajectories-before-building-the-distiller turned "how many recurring
trajectories are there" from a question someone would answer ad hoc into two pure functions, so
the definitions — a turn's sequence, a retry collapsed, recurrence across sessions, and the
would-have-helped ordering — are held here against constructed messages rather than trusted.
"""

from datetime import UTC, datetime

from langchain_core.messages import AIMessage, HumanMessage

from chemclaw.cli.trajectory_census import Turn, census, normalized_tools


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
        _ai("propose_knowledge_note"),
        HumanMessage("thanks — now the solubility"),
        _ai("predict_solubility"),
    ]
    assert normalized_tools(messages) == [
        ("gather_evidence", "compute_xtb_energy", "propose_knowledge_note"),
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
