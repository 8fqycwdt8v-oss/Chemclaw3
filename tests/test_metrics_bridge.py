"""`chemclaw_jobs_started_total` and `chemclaw_notes_recorded_total` are actually incremented.

A declared counter nothing writes reports a flat `0`, a fabricated zero indistinguishable from an
idle service. These tests read the registry value before and after rather than asserting a call.
"""

import asyncio
from types import SimpleNamespace

from chemclaw.core.metrics import METRICS
from chemclaw.kg.note import Note
from chemclaw.kg.record import NoteWrite, WriteOutcome, record_note


class _Submitter:
    """A writer that succeeds, so the count reflects a note that reached the graph."""

    async def write(self, write: NoteWrite) -> WriteOutcome:
        """Return a stable reference without touching git."""
        return WriteOutcome(reference=f"ref:{write.files[-1].path}")


class _NoOpWriter:
    """A writer that succeeds and changes nothing: the byte-identical re-write.

    `GitNoteWriter` returns `written=False` in that case, and the counter means "a note reached the
    graph", so a no-op must not move it. Every other fake returns the default `written=True`.
    """

    async def write(self, write: NoteWrite) -> WriteOutcome:
        """Report the unchanged tree, the way the git writer reports it."""
        return WriteOutcome(reference="main", notes=0)


class _FailingSubmitter:
    """A submitter that raises, standing in for a broken token or unreachable remote."""

    async def write(self, write: NoteWrite) -> WriteOutcome:
        """Fail the way a real submitter fails."""
        raise RuntimeError("git push rejected")


def _agent_note(note_id: str) -> Note:
    """The minimal agent-authored note the gate accepts."""
    return Note(
        id=note_id,
        type="playbook",
        body="something worth reviewing",
        created_by="agent",
    )


def test_a_recorded_note_moves_the_counter() -> None:
    """The count rises by exactly one when a note reaches the graph."""
    before = METRICS.value("chemclaw_notes_recorded_total")
    asyncio.run(record_note(_agent_note("rev19-ok"), _Submitter()))
    assert METRICS.value("chemclaw_notes_recorded_total") == before + 1


def test_a_failed_write_does_not_move_the_counter() -> None:
    """A failed write does not move the notes counter.

    It is incremented after the writer returns, so a failing write path cannot look busy and
    healthy.
    """
    before = METRICS.value("chemclaw_notes_recorded_total")
    try:
        asyncio.run(record_note(_agent_note("rev19-fail"), _FailingSubmitter()))
    except RuntimeError:
        pass
    assert METRICS.value("chemclaw_notes_recorded_total") == before


def test_a_write_that_changed_nothing_does_not_move_the_counter() -> None:
    """A write that changed nothing does not move the notes counter.

    Re-recording a note byte-for-byte is frequent; counting it would turn "notes recorded" into
    "writes attempted", which `tool_usage` already answers.
    """
    before = METRICS.value("chemclaw_notes_recorded_total")
    reference = asyncio.run(record_note(_agent_note("rev19-noop"), _NoOpWriter()))
    assert reference == "main", "the caller still gets the writer's reference"
    assert METRICS.value("chemclaw_notes_recorded_total") == before


def test_a_rejected_human_note_does_not_move_the_counter() -> None:
    """`record_note` refuses human-authored notes before writing, so nothing is counted."""
    human = _agent_note("rev19-human").model_copy(update={"created_by": "human"})
    before = METRICS.value("chemclaw_notes_recorded_total")
    try:
        asyncio.run(record_note(human, _Submitter()))
    except ValueError:
        pass
    assert METRICS.value("chemclaw_notes_recorded_total") == before


def test_the_bridge_tolerates_an_update_that_raises() -> None:
    """A metrics bug costs the metric, never the operation being counted.

    `Metrics.increment` raises on an undeclared name or mismatched labels; the bridge swallows that.
    Asserted with an update that genuinely raises.
    """
    from chemclaw.core.metrics_bridge import record_metric

    record_metric(lambda m: m.increment("no_such_counter_declared_anywhere"))


def test_the_priced_token_dimensions_are_published_separately() -> None:
    """The priced token dimensions are published separately.

    Input, output and cache-read tokens carry different prices, so one total cannot answer cost.
    Driven through `graph_usage_tokens` on a real chunk shape, since reading is where it can go
    wrong.
    """
    from chemclaw.api.runner_usage import graph_usage_tokens

    chunk = SimpleNamespace(
        usage_metadata={
            # LangChain reports `input_tokens` *including* the cached tokens and breaks them out
            # again below, which is why 1050 is the reported input for 100 priced ones.
            "input_tokens": 1050,
            "output_tokens": 20,
            "total_tokens": 1070,
            "input_token_details": {"cache_read": 900, "cache_creation": 50},
        }
    )
    usage = graph_usage_tokens(chunk)

    assert (usage.input, usage.output) == (100, 20)
    # The two that were never read at all. Without them, the 900 cheap tokens above are invisible
    # and the deployment looks like it is paying full price for every one of them.
    assert (usage.cache_read, usage.cache_write) == (900, 50)
    # And the cache counts are *not* left inside `input`: counting them twice — once cheap, once
    # expensive — overstates the priced input of exactly the deployments that cache best, which is
    # the opposite of the mistake this fixes.
    assert usage.input == 100


def test_a_provider_that_reports_cache_outside_its_input_meters_no_negative_input() -> None:
    """A provider reporting cache tokens outside its input meters no negative input.

    Some gateways report cached tokens beside input rather than inside it, so the subtraction is
    clamped at 0; the total, which the budget binds on, is untouched.
    """
    from chemclaw.api.runner_usage import graph_usage_tokens

    chunk = SimpleNamespace(
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 1_070,
            "input_token_details": {"cache_read": 900, "cache_creation": 50},
        }
    )

    usage = graph_usage_tokens(chunk)

    assert usage.input == 0, f"a provider's disagreeing numbers metered {usage.input} input tokens"
    assert usage.total == 1_070, "the clamp changed the total the budget binds on"


def test_an_unreadable_usage_block_is_counted_once_per_chunk() -> None:
    """An unreadable usage block is counted once per chunk.

    `chemclaw_usage_unreadable_total` is incremented by the count and alerted on as a rate, so the
    value is asserted, not just truthiness. Unreadable means usage was reported but could not be
    read (e.g. an upstream rename), distinct from no usage reported.
    """
    from chemclaw.agent.turn_usage import TurnUsage
    from chemclaw.api.runner_usage import graph_usage_tokens

    turn = TurnUsage()
    for _ in range(3):
        turn.add(graph_usage_tokens(SimpleNamespace(usage_metadata={"total_tokens": 0})))
    # A chunk carrying no usage at all is the normal case, not a signal, and must not be counted.
    turn.add(graph_usage_tokens(SimpleNamespace(usage_metadata=None)))

    assert turn.unreadable == 3, (
        f"three unreadable usage blocks were counted as {turn.unreadable}; the counter an operator "
        "alerts on is scaled by whatever this flag happens to be"
    )


def test_a_provider_reporting_no_cache_counts_leaves_those_counters_alone() -> None:
    """A provider reporting no cache counts leaves those counters alone.

    A fabricated zero is indistinguishable from a genuinely uncached deployment.
    """
    from chemclaw.api.runner_usage import graph_usage_tokens
    from chemclaw.core.metrics import METRICS

    chunk = SimpleNamespace(
        usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
    )
    usage = graph_usage_tokens(chunk)
    assert (usage.cache_read, usage.cache_write) == (0, 0)

    before = METRICS.value("chemclaw_cache_read_tokens_total")
    if usage.cache_read:  # the runner's own guard, restated — labels included, as it passes them
        METRICS.increment(
            "chemclaw_cache_read_tokens_total", float(usage.cache_read), {"profile": "default"}
        )
    assert METRICS.value("chemclaw_cache_read_tokens_total") == before
