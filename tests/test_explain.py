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

from langchain_core.messages import AIMessage, HumanMessage, message_to_dict

from chemclaw.agent.message_migration import LANGCHAIN_SHAPE
from chemclaw.cli.explain import Job, ToolCall, _render, _speaker
from tests.legacy_rows import legacy_text

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
            "c-4": [ToolCall("sample_conformers", "error", "cluster unreachable", 9.0, "u", "")]
        },
    )
    assert "sample_conformers" in report and "error" in report


def test_pre_join_rows_are_labelled_rather_than_silently_grouped() -> None:
    """Rows written before the migration carry an empty correlation id and must not look attributed.

    Collapsing them into an unlabelled group would present unrelated turns as one conversation —
    inventing a relationship the data does not support, which is the failure the migration
    deliberately avoided by not backfilling.
    """
    report = _report(order=[""], turns={"": [("user", "an older turn")]})
    assert "unattributed" in report


def test_both_stored_shapes_are_read_because_the_table_holds_both() -> None:
    """The defect this pins: the CLI read the legacy shape only, so every current row was blank.

    `session_messages` holds two serializations — the framework layer 1 was first built on wrote
    one, LangChain writes the other, and M6's conversion pass is resumable — so an audit
    reconstruction that knows one of them silently shows an empty conversation for exactly the
    sessions still in use. Asserted from both sides, because reading only the *new* shape would be
    the same bug pointed at the archive.
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
    """A reconstruction must not print a guess as the record.

    `message_from_row` never raises — the read path is deliberately forgiving, because one
    unreadable historical row must not cost a chemist the whole conversation — so what comes back
    for a row it could not convert is prose under a speaker *guessed* from whichever label the row
    happens to carry. For the transcript route that is the right answer. Here it is not: this
    report is evidence, and a guessed speaker printed beside a real one is indistinguishable from
    it. The store marks what it recovered; this asserts the marker is read.

    The `except` below it stays for a payload that cannot even be rendered, which is why this is
    asserted through `_speaker` rather than by reading the branch.
    """
    recovered, _ = _speaker({"role": "assistant", "contents": ["not a content part"]})
    assert recovered == "unknown", "a recovered row was attributed to a speaker nobody established"
    # The contrast is the whole assertion: a row that *did* convert keeps its real speaker, so this
    # cannot be satisfied by calling everything unknown.
    assert _speaker(legacy_text("user", "hello")) == ("user", "hello")


def test_a_turn_with_both_a_tool_call_and_a_job_is_rendered_once() -> None:
    """The routine post-retention case, rendered twice — one occurrence read as two.

    `shown` de-duplicated `(*calls, *jobs)` against the transcript's `order` and never against
    itself, so a turn present in *both* key sequences and absent from the transcript appeared
    twice: same header, same job line, same tool line. That is not a corner — `_render`'s own
    docstring says the trail routinely outlives the words it points at, because `durable/retention`
    prunes `session_messages` by age and an abandoned turn never writes a transcript row at all. A
    reviewer asking "why was this run?" on a session older than the message-retention window was
    shown one durable job and one tool call as two separate occurrences.
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
    """The `("unknown", <repr>)` fallback this function documents was unreachable in practice.

    `_speaker`'s docstring promises "an unreadable payload renders as its repr under an `unknown`
    role rather than raising" — the promise that matters after the blank-transcript defect
    `CLAUDE.md` records. But `message_from_row` catches internally and returns a *degraded* message
    rather than raising, so the `except` arm is dead for a dict payload; a payload with no
    recoverable prose came back as an empty message and `explain` dropped the row with `if text:`.
    The turn then rendered "transcript: absent (compacted, pruned, or rolled back)" — a specific,
    and wrong, explanation of a row that is on disk and merely unreadable.

    The column is bare `jsonb`, so a shape neither reader recognises is exactly what this is for.
    """
    role, text = _speaker({"nope": 1}, None)
    assert role == "unknown"
    assert text, "an unrenderable row rendered as nothing and would be silently dropped"
    assert "nope" in text

    # And the reconstruction distinguishes the two, which is the point: one turn holds an
    # unreadable row, the other holds no row at all.
    report = _report(order=["turn-a"], turns={"turn-a": [("unknown", "{'nope': 1}")]})
    assert "transcript: absent" not in report, report
