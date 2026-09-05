"""The two counters that were declared and never incremented (REV-19, D-136).

`chemclaw_jobs_started_total` and `chemclaw_notes_proposed_total` were in `core/metrics.py`'s
declaration table and written by nothing, so every scrape reported a flat `0`. That is worse than
omitting them: the module's gauge path explicitly refuses to emit an unbound gauge because "a
fabricated zero would be indistinguishable from a genuinely idle service", and these counters had
exactly that failure with no such protection. A PR-gate rejecting every note looked identical to a
quiet afternoon.

These tests read the registry value before and after, so they fail on the unfixed code. Asserting
that some function *was called* would have passed against a counter nobody ever read.
"""

import asyncio
from types import SimpleNamespace

from chemclaw.core.metrics import METRICS
from chemclaw.kg.note import Note
from chemclaw.kg.record import NoteWrite, WriteOutcome, record_note


class _Submitter:
    """A submitter that succeeds, so the count reflects a note that reached the branch."""

    async def submit(self, submission: NoteWrite) -> WriteOutcome:
        """Return a stable reference without touching git."""
        return WriteOutcome(reference=f"ref:{submission.branch}")


class _FailingSubmitter:
    """A submitter that raises, standing in for a broken token or unreachable remote."""

    async def submit(self, submission: NoteWrite) -> WriteOutcome:
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


def test_a_proposed_note_moves_the_counter() -> None:
    """The count rises by exactly one when a note reaches the branch."""
    before = METRICS.value("chemclaw_notes_proposed_total")
    asyncio.run(record_note(_agent_note("rev19-ok"), _Submitter()))
    assert METRICS.value("chemclaw_notes_proposed_total") == before + 1


def test_a_failed_submission_does_not_move_the_counter() -> None:
    """A PR-gate that is failing every write must not report healthy.

    This is the whole point of the counter, and the reason it is incremented *after* the submitter
    returns rather than before: counting the attempt would show a busy, working gate during exactly
    the outage the metric exists to reveal.
    """
    before = METRICS.value("chemclaw_notes_proposed_total")
    try:
        asyncio.run(record_note(_agent_note("rev19-fail"), _FailingSubmitter()))
    except RuntimeError:
        pass
    assert METRICS.value("chemclaw_notes_proposed_total") == before


def test_a_rejected_human_note_does_not_move_the_counter() -> None:
    """The gate refuses human-authored notes before submitting, so nothing is counted."""
    human = _agent_note("rev19-human").model_copy(update={"created_by": "human"})
    before = METRICS.value("chemclaw_notes_proposed_total")
    try:
        asyncio.run(record_note(human, _Submitter()))
    except ValueError:
        pass
    assert METRICS.value("chemclaw_notes_proposed_total") == before


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
