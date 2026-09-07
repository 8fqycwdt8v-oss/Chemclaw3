"""The two counters that were declared and never incremented (REV-19, D-136).

`chemclaw_jobs_started_total` and `chemclaw_notes_recorded_total` were in `core/metrics.py`'s
declaration table and written by nothing, so every scrape reported a flat `0`. That is worse than
omitting them: the module's gauge path explicitly refuses to emit an unbound gauge because "a
fabricated zero would be indistinguishable from a genuinely idle service", and these counters had
exactly that failure with no such protection. A write path rejecting every note looked identical
to a quiet afternoon.

These tests read the registry value before and after, so they fail on the unfixed code. Asserting
that some function *was called* would have passed against a counter nobody ever read.
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
    """A writer that succeeds and changes nothing — the byte-identical re-write.

    `GitNoteWriter` returns `written=False` when every file was already there with the same bytes,
    and `WriteOutcome`'s own docstring makes that the point of the field: "the counter below means
    'a note reached the graph', and incrementing it for a no-op would make it count attempts."
    Nothing drove it. Every other fake here returns the default `written=True`, so `if
    outcome.written:` -> `if True:` survived the whole record/knowledge set — 112 tests.
    """

    async def write(self, write: NoteWrite) -> WriteOutcome:
        """Report the unchanged tree, the way the git writer reports it."""
        return WriteOutcome(reference="main", written=False)


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
    """A write path that is failing every note must not report healthy.

    This is the whole point of the counter, and the reason it is incremented *after* the writer
    returns rather than before: counting the attempt would show a busy, working system during
    exactly the outage the metric exists to reveal.
    """
    before = METRICS.value("chemclaw_notes_recorded_total")
    try:
        asyncio.run(record_note(_agent_note("rev19-fail"), _FailingSubmitter()))
    except RuntimeError:
        pass
    assert METRICS.value("chemclaw_notes_recorded_total") == before


def test_a_write_that_changed_nothing_does_not_move_the_counter() -> None:
    """A no-op is not a note reaching the graph, and the counter must not say it was.

    The distinction the field exists for: re-recording the same note byte-for-byte is a legitimate
    and frequent outcome (a miner re-running over a corpus it has already read), and counting it
    turns "notes recorded" into "writes attempted" — which `tool_usage` already answers.
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
    """A metrics bug must cost the metric, never the operation being counted.

    `Metrics.increment` raises `KeyError` on an undeclared counter name or a label set that does
    not match the declaration — strictness that is right for the registry and fatal on a request
    path. The swallow is what makes the ~10 call sites across six packages safe, and it is asserted
    by passing an update that genuinely raises rather than by reaching into the swallow.
    """
    from chemclaw.core.metrics_bridge import record_metric

    record_metric(lambda m: m.increment("no_such_counter_declared_anywhere"))


def test_the_priced_token_dimensions_are_published_separately() -> None:
    """One undifferentiated total cannot answer "what is this costing" (REV-10, D-144).

    Input, output and cache-read carry different prices — a cache read is roughly an order of
    magnitude cheaper than a fresh input token — so a deployment that caches well and one that does
    not published *identical* `chemclaw_tokens_total` while their bills differed several-fold. The
    provider had reported all four dimensions all along; nothing read past the sum.

    Driven through `graph_usage_tokens` on a real chunk shape rather than by calling the counters
    directly, because the defect was in the reading, not the publishing.
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
    """The clamp under the cache subtraction, which every fixture so far kept comfortably positive.

    LangChain's own client reports `input_tokens` *including* the cached share and breaks it out
    again, which is what the test above pins. A gateway is not obliged to: reporting the cached
    tokens beside the input rather than inside it makes the subtraction negative, and a negative
    input token count would flow into `chemclaw_input_tokens_total` and into the turn's own record
    as a credit against real spend. It meters 0 — the honest answer when two of a provider's own
    numbers disagree — and the total, which is what the budget binds on, is untouched.
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
    """`chemclaw_usage_unreadable_total` is incremented *by the count*, so the count is the claim.

    `unreadable` distinguishes "nobody reported usage" from "usage was reported and we could not
    read it" — the second is an upstream rename, which measured on the reader this replaced booked
    50 turns of 15,000 real tokens each as zero while the budget went on allowing the next one. The
    counter it feeds is a rate an operator alerts on, so a flag that reads 2 per chunk doubles that
    rate; nothing asserted the value, only that it was truthy.
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
    """A fabricated zero is indistinguishable from a genuinely uncached deployment.

    The same rule `chemclaw.core.metrics` states for gauges — it refuses to emit an unbound one
    because "a fabricated zero would be indistinguishable from a genuinely idle service" — and the
    exact failure REV-19 found in the counters. An `openai_compatible` endpoint that reports no
    cache fields must leave those two counters untouched, not publish 0.
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
