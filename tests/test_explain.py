"""`chemclaw explain` answers "why was this run?" from the join.

The join is the two columns added by D-2026-07-31-the-audit-chain-is-versioned.

The columns are the mechanism; this is the claim. A test that only asserted the columns exist would
pass while the question stayed unanswerable, which is the failure mode this whole line of work is
about — so these drive the renderer with the shapes a real trail contains: a turn whose transcript
survived, a durable job that stated its reason, and a turn whose words were compacted away while
its tool calls remained.

`_render` is separated from the fetch precisely so this runs with no database, which matters
because the Postgres-backed tests skip in an offline sandbox and a reconstruction tool that is only
exercised in CI is one nobody has actually read the output of.
"""

from chemclaw.cli.explain import Job, ToolCall, _render, _speaker

_SESSION = "s-42"


def _report(
    *,
    order: list[str] | None = None,
    turns: dict[str, list[tuple[str, str]]] | None = None,
    calls: dict[str, list[ToolCall]] | None = None,
    jobs: dict[str, list[Job]] | None = None,
) -> str:
    """Render one session's reconstruction as a single string for substring assertions."""
    return "\n".join(_render(_SESSION, order or [], turns or {}, calls or {}, jobs or {}))


def test_a_tool_call_is_printed_under_the_question_that_caused_it() -> None:
    """The whole point: the words and the tool calls appear together, keyed by the turn.

    Before D-2026-07-31-the-audit-chain-is-versioned
    these lived in two tables with no key between them, so this report could not have
    been written at all — the audit row knew its correlation id and nothing knew which conversation
    that id belonged to.
    """
    report = _report(
        order=["c-1"],
        turns={"c-1": [("user", "is 2-MeTHF a sane swap for THF here?")]},
        calls={"c-1": [ToolCall("predict_solubility", "ok", "", 42.0, "u-1", "")]},
    )
    question_at = report.index("2-MeTHF a sane swap")
    tool_at = report.index("predict_solubility")
    assert question_at < tool_at, "the tool call must be attributed to the question above it"


def test_a_durable_job_prints_the_reason_its_launcher_had_to_state() -> None:
    """D-157 made a launch state its rationale; this is where that pays off, verbatim."""
    report = _report(
        order=["c-2"],
        turns={"c-2": [("user", "what should we run next on the biaryl?")]},
        jobs={
            "c-2": [
                Job("bo", "campaign", "narrow the base/solvent space before scale-up", "best 79%")
            ]
        },
    )
    assert "because: narrow the base/solvent space before scale-up" in report
    assert "best 79%" in report


def test_a_turn_whose_words_were_compacted_away_is_still_shown() -> None:
    """The honest case, documenting a remaining limit.

    The limit belongs to D-2026-07-31-the-audit-chain-is-versioned.

    Retention prunes message rows by age, and a turn that ran its tools and then failed never
    writes a transcript row at all — so the trail can outlive the conversation it points at.
    Dropping such a turn would hide exactly the evidence an auditor needs; saying the transcript
    is gone is the truthful rendering.
    """
    report = _report(order=[], calls={"c-3": [ToolCall("expand_note", "ok", "", 5.0, "u-1", "")]})
    assert "expand_note" in report
    assert "transcript: absent" in report


def test_an_empty_session_says_so_rather_than_printing_nothing() -> None:
    """Silence reads as a broken tool; an explicit statement reads as an answer."""
    assert "no messages, tool calls or jobs recorded" in _report(order=[])


def test_a_failed_tool_call_is_not_hidden() -> None:
    """A call that failed is the most interesting row in a reconstruction, not one to omit."""
    report = _report(
        order=["c-4"],
        turns={"c-4": [("user", "compute the barrier")]},
        calls={
            "c-4": [ToolCall("compute_dft_energy", "error", "cluster unreachable", 9.0, "u", "")]
        },
    )
    assert "compute_dft_energy" in report and "error" in report


def test_pre_join_rows_are_labelled_rather_than_silently_grouped() -> None:
    """Rows written before the migration carry an empty correlation id and must not look attributed.

    Collapsing them into an unlabelled group would present unrelated turns as one conversation —
    inventing a relationship the data does not support, which is the failure the migration
    deliberately avoided by not backfilling.
    """
    report = _report(order=[""], turns={"": [("user", "an older turn")]})
    assert "unattributed" in report


def test_a_message_shape_this_tool_did_not_write_does_not_crash_it() -> None:
    """`Message.to_dict()` is upstream's shape; a reconstruction must degrade, not fail.

    The row is still evidence that something was said, and reporting it as unparsed is more useful
    than a traceback that hides every other turn in the session.
    """
    assert _speaker({"role": "user", "contents": [{"text": "hello"}]}) == ("user", "hello")
    assert _speaker({"role": "assistant", "text": "hi"}) == ("assistant", "hi")
    assert _speaker("not a dict at all")[0] == "unknown"
    assert _speaker({"role": "user", "contents": [{"image": "x"}]}) == ("user", "")
