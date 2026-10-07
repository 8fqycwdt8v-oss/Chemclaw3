"""`chemclaw explain` answers "why was this run?" by joining the transcript and the audit trail.

Drives the renderer with the shapes a real trail contains: a turn whose transcript survived, a
durable job that stated its reason, and a turn whose words were compacted away while its tool
calls remained. `_render` is separated from the fetch so this runs with no database.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, message_to_dict

from chemclaw.agent.message_migration import LANGCHAIN_SHAPE
from chemclaw.cli.explain import (
    Job,
    ToolCall,
    TurnEnd,
    _is_database_refusal,
    _render,
    _speaker,
)
from chemclaw.core.config import settings
from tests.legacy_rows import legacy_text

_SESSION = "s-42"


def _report(
    *,
    order: list[str] | None = None,
    turns: dict[str, list[tuple[str, str]]] | None = None,
    calls: dict[str, list[ToolCall]] | None = None,
    jobs: dict[str, list[Job]] | None = None,
    ends: dict[str, TurnEnd] | None = None,
    known: bool = False,
) -> str:
    """Render one session's reconstruction as a single string for substring assertions."""
    return "\n".join(
        _render(
            _SESSION,
            order or [],
            turns or {},
            calls or {},
            jobs or {},
            ends or {},
            known=known,
        )
    )


def test_a_tool_call_is_printed_under_the_question_that_caused_it() -> None:
    """A tool call is printed under the question that caused it, keyed by the turn."""
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
    """A turn whose words were compacted away is still shown.

    Retention prunes messages by age, and a turn that failed after its tools never writes a
    transcript row, so the trail can outlive the conversation. Saying the transcript is gone is the
    truthful rendering.
    """
    report = _report(order=[], calls={"c-3": [ToolCall("expand_note", "ok", "", 5.0, "u-1", "")]})
    assert "expand_note" in report
    assert "transcript: absent" in report


def test_an_empty_session_says_so_rather_than_printing_nothing() -> None:
    """An empty session says so rather than printing nothing.

    The line names `turn_costs` too, since it is a fourth source of turns.
    """
    assert "no messages, tool calls, jobs or turn records" in _report(order=[])


def test_a_failed_tool_call_is_not_hidden() -> None:
    """A call that failed is the most interesting row in a reconstruction, not one to omit."""
    report = _report(
        order=["c-4"],
        turns={"c-4": [("user", "compute the barrier")]},
        calls={
            "c-4": [ToolCall("sample_conformers", "error", "cluster unreachable", 9.0, "u", "")]
        },
    )
    assert "sample_conformers" in report and "error" in report


def test_pre_join_rows_are_labelled_rather_than_silently_grouped() -> None:
    """Rows written before the join columns existed are labelled rather than silently grouped.

    Grouping unrelated turns would invent a relationship the data does not support.
    """
    report = _report(order=[""], turns={"": [("user", "an older turn")]})
    assert "unattributed" in report


def test_both_stored_shapes_are_read_because_the_table_holds_both() -> None:
    """Both stored message shapes are read, because the table holds both.

    Reading only one shape silently shows an empty conversation for the sessions in the other.
    """
    assert _speaker(legacy_text("user", "hello")) == ("user", "hello")
    assert _speaker(message_to_dict(HumanMessage(content="hello")), LANGCHAIN_SHAPE) == (
        "user",
        "hello",
    )
    assert _speaker(message_to_dict(AIMessage(content="hi")), LANGCHAIN_SHAPE) == (
        "assistant",
        "hi",
    )


def test_a_message_shape_this_tool_did_not_write_does_not_crash_it() -> None:
    """A stored shape is upstream's to change; a reconstruction must degrade, not fail.

    The row is still evidence that something was said, and reporting it as unparsed is more useful
    than a traceback that hides every other turn in the session.
    """
    assert _speaker("not a dict at all")[0] == "unknown"
    # A legacy row carrying no prose at all — an image part, say — has a role and nothing to say.
    assert _speaker({"role": "user", "contents": [{"type": "text", "text": ""}]}) == ("user", "")


def test_a_row_the_store_could_only_recover_is_not_attributed_to_a_speaker() -> None:
    """A row the store could only recover is not attributed to a speaker.

    `message_from_row` never raises and recovers prose under a guessed speaker, which is right for
    the transcript route but not for evidence. The store marks what it recovered and `_speaker`
    reads the marker; the `except` remains for a payload that cannot be rendered at all.
    """
    recovered, _ = _speaker({"role": "assistant", "contents": ["not a content part"]})
    assert recovered == "unknown", "a recovered row was attributed to a speaker nobody established"
    # The contrast is the whole assertion: a row that *did* convert keeps its real speaker, so this
    # cannot be satisfied by calling everything unknown.
    assert _speaker(legacy_text("user", "hello")) == ("user", "hello")


def test_a_turn_with_both_a_tool_call_and_a_job_is_rendered_once() -> None:
    """A turn with both a tool call and a job, and no transcript, is rendered once.

    The routine post-retention case: de-duplication must cover the turn keys of calls and jobs
    together, not only against the transcript's order.
    """
    correlation = "turn-1"
    report = _report(
        calls={correlation: [ToolCall("similar_molecules", "ok", "", 1.0, "alice", "precedent")]},
        jobs={correlation: [Job("calc", "compute_thermochemistry", "the barrier", "done")]},
    )
    assert report.count(f"── turn {correlation}") == 1, report
    assert report.count("job calc:compute_thermochemistry") == 1, report
    assert report.count("tool similar_molecules") == 1, report


def test_an_unrenderable_row_shows_its_repr_instead_of_reading_as_an_absent_transcript() -> None:
    """An unrenderable row shows its repr instead of reading as an absent transcript.

    `message_from_row` returns a degraded message rather than raising, so an empty one must still be
    rendered under `unknown` rather than dropped and explained as compacted or pruned. The column is
    bare `jsonb`, so unrecognised shapes are expected.
    """
    role, text = _speaker({"nope": 1}, None)
    assert role == "unknown"
    assert text, "an unrenderable row rendered as nothing and would be silently dropped"
    assert "nope" in text

    # And the reconstruction distinguishes the two, which is the point: one turn holds an
    # unreadable row, the other holds no row at all.
    report = _report(order=["turn-a"], turns={"turn-a": [("unknown", "{'nope': 1}")]})
    assert "transcript: absent" not in report, report


def test_a_helpers_call_is_marked_as_the_helpers_and_the_chemists_own_is_not() -> None:
    """A helper's call is marked as the helper's; the chemist's own read as before.

    A helper acts on a brief the chemist never saw, so its calls must not read as the chemist's.
    Empty `agent` means the agent the chemist talked to and adds nothing to the line.
    """
    report = _report(
        order=["c-1"],
        calls={
            "c-1": [
                ToolCall("task", "ok", "", 12.0, "alice@corp", "", ""),
                ToolCall("find_notes", "ok", "", 8.0, "alice@corp", "", "default-helper"),
            ]
        },
    )
    assert "tool find_notes [ok, 8 ms, alice@corp via default-helper]" in report
    assert "tool task [ok, 12 ms, alice@corp]" in report


def test_an_empty_report_says_why_it_is_empty_rather_than_only_that_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty report says why it is empty.

    A typo'd id, a deployment that records nothing, and a pruned session must be distinguishable.
    """
    monkeypatch.setattr(settings, "session_store", "memory")
    assert "CHEMCLAW_SESSION_STORE=memory" in _report(order=[])

    monkeypatch.setattr(settings, "session_store", "postgres")
    unknown = _report(order=[], known=False)
    assert "was ever created against this database" in unknown
    pruned = _report(order=[], known=True)
    assert "session_owners has its row" in pruned
    assert "retention has pruned it" in pruned


def test_a_session_with_rows_is_not_given_an_explanation_it_does_not_need() -> None:
    """The explanation belongs to the empty case only — a report with turns is its own answer."""
    report = _report(order=["c-1"], calls={"c-1": [ToolCall("find_notes", "ok", "", 3.0, "u", "")]})
    assert "CHEMCLAW_SESSION_STORE" not in report
    assert "was ever created against this database" not in report


# --- how the turn ended, which nothing asked ------------------------------------------------------


def test_a_capped_turn_stops_reading_as_a_clean_one() -> None:
    """A capped turn does not read as a clean one.

    The outcome is stored in `turn_costs` on the key this report groups by, so the report reads it.
    """
    clean = _report(
        order=["c-1"],
        turns={"c-1": [("assistant", "FINAL: yes, with a peroxide check.")]},
        ends={"c-1": TurnEnd(outcome="answered")},
    )
    capped = _report(
        order=["c-1"],
        turns={"c-1": [("assistant", "Still checking; one more source.")]},
        ends={"c-1": TurnEnd(outcome="loop_capped")},
    )

    assert "ended: answered" in clean
    assert "ended: loop_capped" in capped
    # And a clean turn still says how it ended, because a marker whose absence means nothing is
    # not a marker.
    assert "ended:" in clean


def test_the_endings_line_names_every_stored_qualifier_it_has() -> None:
    """The endings line names every stored qualifier it has.

    `compacted`, `context_unreducible`, `answer_confidence`, `review_required`, `completed` and
    `error_code` beside `outcome`, so e.g. an answer from an unfittable thread is visible.
    """
    line = TurnEnd(
        outcome="errored",
        completed=False,
        compacted=True,
        context_unreducible=True,
        answer_confidence=0.41,
        review_required=True,
        error_code="upstream_timeout",
    ).line()

    assert "ended: errored" in line
    assert "no answer delivered" in line
    assert "error upstream_timeout" in line
    assert "context compacted" in line
    assert "unreducible" in line
    assert "confidence 0.41" in line
    assert "flagged for review" in line
    # A clean turn's line carries no parenthetical at all.
    assert TurnEnd(outcome="answered").line() == "   ended: answered"


def test_a_cancelled_turn_is_a_turn_rather_than_three_guesses() -> None:
    """A cancelled turn is shown as a turn rather than three guesses.

    A turn that wrote only a cost row is recorded in `turn_costs`, so the ledger is a fourth source
    of turns and a stored fact is not rendered as a guess.
    """
    report = _report(known=True, ends={"c-9": TurnEnd(outcome="abandoned", completed=False)})

    assert "── turn c-9" in report
    assert "ended: abandoned (no answer delivered)" in report
    assert "retention has pruned it" not in report
    assert "no messages, tool calls, jobs or turn records" not in report


def test_a_tool_result_does_not_render_as_something_somebody_said() -> None:
    """A tool result does not render as something somebody said.

    The body is printed, since the audit row never records what a call returned, but as a result
    beside the call, not as a speaker.
    """
    report = _report(
        order=["c-1"],
        turns={
            "c-1": [("tool", "{'flags': ['peroxide former']}"), ("assistant", "yes, carefully")]
        },
        calls={"c-1": [ToolCall("screen_hazards", "ok", "", 42.0, "u-1", "")]},
    )

    assert "   tool result: {'flags': ['peroxide former']}" in report
    assert "   tool: {'flags'" not in report
    # The audit line is untouched — it is the other rendering, and it is the one that is a record.
    assert "tool screen_hazards [ok, 42 ms, u-1]" in report


def test_a_database_that_answers_and_refuses_is_told_apart_from_one_that_does_not() -> None:
    """A database that answers and refuses is told apart from one that does not answer.

    A schema mismatch is a `ProgrammingError`, not a `ConnectionError`. Duck-typed on the driver's
    `sqlstate`/`diag` pair because `chemclaw.cli` may not import `psycopg`
    (`tests/test_third_party_layering.py`).
    """
    import psycopg

    assert _is_database_refusal(psycopg.errors.UndefinedTable("no such table"))
    assert _is_database_refusal(psycopg.ProgrammingError("bad query"))
    assert _is_database_refusal(psycopg.OperationalError("gone"))
    # Everything else still raises, so a programming error in this file is not swallowed as "the
    # database refused".
    assert not _is_database_refusal(ValueError("a bug in this module"))
    assert not _is_database_refusal(KeyError("c-1"))


def test_an_errored_turn_says_why_its_transcript_is_absent_rather_than_guessing() -> None:
    """An errored turn says why its transcript is absent rather than guessing.

    `endings` already holds the outcome on the same key, so an errored or abandoned turn says so
    instead of pointing at compaction, retention or a rollback.
    """
    errored = _report(order=["c-1"], turns={}, ends={"c-1": TurnEnd(outcome="errored")})

    assert "transcript: absent (the turn ended errored before one was written)" in errored, errored
    assert "compacted, pruned, or rolled back" not in errored, (
        "an errored turn is still offered three causes it is not, one line above the ledger row "
        f"that names the real one:\n{errored}"
    )
    # The guess is right where nothing recorded an ending, and it stays.
    unknown = _report(order=["c-2"], turns={}, ends={})
    assert "transcript: absent (compacted, pruned, or rolled back)" in unknown
