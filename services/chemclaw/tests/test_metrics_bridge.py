"""The two counters that were declared and never incremented (REV-19, D-136).

`chemclaw_jobs_started_total` and `chemclaw_notes_proposed_total` were in `service/metrics.py`'s
declaration table and written by nothing, so every scrape reported a flat `0`. That is worse than
omitting them: the module's gauge path explicitly refuses to emit an unbound gauge because "a
fabricated zero would be indistinguishable from a genuinely idle service", and these counters had
exactly that failure with no such protection. A PR-gate rejecting every note looked identical to a
quiet afternoon.

These tests read the registry value before and after, so they fail on the unfixed code. Asserting
that some function *was called* would have passed against a counter nobody ever read.
"""

import asyncio

from kg.note import Note
from kg.pr_gate import NoteSubmission, propose_note
from service.metrics import METRICS


class _Submitter:
    """A submitter that succeeds, so the count reflects a note that reached the branch."""

    async def submit(self, submission: NoteSubmission) -> str:
        """Return a stable reference without touching git."""
        return f"ref:{submission.branch}"


class _FailingSubmitter:
    """A submitter that raises, standing in for a broken token or unreachable remote."""

    async def submit(self, submission: NoteSubmission) -> str:
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
    asyncio.run(propose_note(_agent_note("rev19-ok"), _Submitter()))
    assert METRICS.value("chemclaw_notes_proposed_total") == before + 1


def test_a_failed_submission_does_not_move_the_counter() -> None:
    """A PR-gate that is failing every write must not report healthy.

    This is the whole point of the counter, and the reason it is incremented *after* the submitter
    returns rather than before: counting the attempt would show a busy, working gate during exactly
    the outage the metric exists to reveal.
    """
    before = METRICS.value("chemclaw_notes_proposed_total")
    try:
        asyncio.run(propose_note(_agent_note("rev19-fail"), _FailingSubmitter()))
    except RuntimeError:
        pass
    assert METRICS.value("chemclaw_notes_proposed_total") == before


def test_a_rejected_human_note_does_not_move_the_counter() -> None:
    """The gate refuses human-authored notes before submitting, so nothing is counted."""
    human = _agent_note("rev19-human").model_copy(update={"created_by": "human"})
    before = METRICS.value("chemclaw_notes_proposed_total")
    try:
        asyncio.run(propose_note(human, _Submitter()))
    except ValueError:
        pass
    assert METRICS.value("chemclaw_notes_proposed_total") == before


def test_the_bridge_tolerates_a_registry_that_cannot_be_imported() -> None:
    """A worker process has no front door; recording a metric there must be a no-op, not a crash.

    The bridge is imported by `connectors` and `kg`, which Temporal workers load without ever
    building `service`. Passing an update that would fail against a real registry proves the
    swallow works without asserting on the swallow's internals.
    """
    from chemclaw.metrics_bridge import record_metric

    record_metric(lambda m: m.increment("no_such_counter_declared_anywhere"))
