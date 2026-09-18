"""Today's workflow code still accepts the histories the shipped code wrote.

The control `durable/connector_job.py` has asked for since it was written, and the defect it was
asked for is measured in `D-2026-09-09-a-replay-control-needs-an-archived-history-not-a-patch`:
between two merged commits `TemplateWorkflow` gained two `record_job` dispatches, and a history
recorded before that change no longer replays against the code after it.

Why a *closed* history is the right thing to check, when a closed run is never resumed in
production: a closed history is the complete record of every command the shipped code emitted, so
replaying it detects a divergence anywhere in the sequence — including the early positions that an
*unfinished* run really does replay through when the background worker is replaced. It is a
deliberately conservative proxy. A red result here is a question ("where does the sequence
diverge?"), not automatically an outage.

`tests/recorded_workflow_histories.py` holds the fixtures, how they were recorded, and the rule for
re-recording one.
"""

import asyncio
import logging

import pytest

from tests.recorded_workflow_histories import (
    ArchivedHistory,
    archived_histories,
    replay_failure,
    superseded_histories,
)

# The workflows on core's `background-jobs` queue with no archived history, and therefore no
# guard against a redeploy that changes their command sequence. Declared rather than counted, for
# the reason `tests/test_context_floor.py` declares `SERVED_ELSEWHERE`: a gap a machine can see is
# a gap somebody closes, and a gap only prose mentions is one a reader assumes is covered. Adding a
# workflow to core's queue means either recording a fixture for it or adding its name here.
#
# `background-jobs` and not every queue, deliberately. This is a deploy-safety control and the
# background worker is the sharp case: `deployment-workers.yaml` deploys it `Recreate`, so the new
# generation is handed every unfinished run the old one held. The connector workers keep the
# default rolling update for reasons their own template argues.
UNCOVERED_BACKGROUND_WORKFLOWS = frozenset(
    {
        "ArtifactEvictionWorkflow",
        "AwaitAnswerWorkflow",
        "CampaignSynthesisWorkflow",
        # New in `D-2026-09-15-the-requester-hears-nothing-until-it-is-too-late`, and uncovered for
        # a reason none of the others has: this control catches today's code refusing a
        # history the *shipped* code wrote, and `recorded_workflow_histories.py` records "from the
        # shape this repository ships". `CheckInWorkflow` has never shipped, so there is no older
        # command sequence it could be incompatible with, and a fixture recorded from it now would
        # be the self-certifying shape that module's docstring rejects — "a test that runs a
        # workflow and then replays the history it just produced compares code against a history
        # that same code wrote". It earns a fixture at its first change, not at its first commit.
        "CheckInWorkflow",
        "CommitmentSyncWorkflow",
        "ConnectorJobWorkflow",
        "DevelopmentReportWorkflow",
        "DigestWorkflow",
        "DocumentShareSyncWorkflow",
        "ElnSyncWorkflow",
        "EvalDriftWorkflow",
        "NoteReindexWorkflow",
        "ObservationPromotionWorkflow",
        "ObservationSynthesisWorkflow",
        "OptimizationCampaignWorkflow",
        "PlaybookDistillationWorkflow",
        "PublishNoteWorkflow",
        "PublishResultsWorkflow",
        "ReactionCorpusWorkflow",
        "ReactionLabelWorkflow",
        "ReportSectionWorkflow",
        "RetentionWorkflow",
    }
)


def _background_workflows() -> dict[str, type]:
    """Core's registered workflows by the name Temporal advertises them under."""
    # Imported for the registration side effect: the registry is populated at import time, so a
    # module nobody imported contributes nothing (`durable/registry.py`).
    import chemclaw.durable.background_worker  # noqa: F401 - registration
    from chemclaw.durable.registry import registered_workflows, temporal_name

    return {
        temporal_name(cls): cls
        for cls in registered_workflows("background")
        if getattr(cls, "__temporal_workflow_definition", None) is not None
    }


def _workflow_for(archived: ArchivedHistory) -> type:
    """The class a fixture's history belongs to, or a failure that says why there is none.

    Named rather than a bare subscript so a fixture recorded from a *bundle's* workflow fails
    with the reason instead of a `KeyError`: this control is scoped to core's queue, and a history
    from somewhere else is a scoping decision to make, not a lookup that went wrong.
    """
    workflows = _background_workflows()
    assert archived.workflow_type in workflows, (
        f"{archived} records `{archived.workflow_type}`, which is not on core's `background` "
        "queue. This control is scoped to that queue because it is the one deployed `Recreate`; "
        "covering a bundle's workflow means widening the scope deliberately, here and in "
        "`UNCOVERED_BACKGROUND_WORKFLOWS`."
    )
    return workflows[archived.workflow_type]


@pytest.mark.parametrize("archived", archived_histories(), ids=str)
def test_an_archived_history_still_replays_against_todays_code(
    archived: ArchivedHistory,
) -> None:
    """A history the shipped code wrote must still be a history this code can replay.

    When this goes red the change under review alters the sequence of commands the workflow
    issues. The two honest responses are in `recorded_workflow_histories`' docstring; silently
    re-recording the fixture is not one of them, because it turns a control into a copy of
    whatever was committed last.
    """
    workflow_class = _workflow_for(archived)
    failure = asyncio.run(replay_failure(archived, workflow_class))
    assert not failure, (
        f"{archived} no longer replays against {archived.workflow_type}: {failure}\n"
        "This is a redeploy hazard, not a test fixture problem: the background worker is deployed "
        "`Recreate`, so the next generation inherits every unfinished run of the old one. Gate the "
        "change with `workflow.patched`, or re-record the fixture and say in the PR why no run of "
        "the old shape can still be in flight."
    )


@pytest.mark.parametrize("archived", superseded_histories(), ids=str)
def test_the_control_still_detects_the_divergence_it_was_built_for(
    archived: ArchivedHistory,
) -> None:
    """A history this code is known to diverge from must come back as a divergence.

    The test above can only ever report that nothing broke, which is exactly what it would report
    if the replay were silently doing nothing at all. `superseded/` holds the measured case — a
    `TemplateWorkflow` history from before `record_job` was dispatched on both endings — so the
    detector is asserted against a real divergence rather than trusted.

    If this ever goes green, today's code has become able to replay the pre-`record_job` shape.
    That is a real finding and not a fixture to delete: it means the two `_record_run` dispatches
    are gone or are now gated, and the change that did it should say so.
    """
    workflow_class = _workflow_for(archived)
    failure = asyncio.run(replay_failure(archived, workflow_class))
    assert failure, (
        f"{archived} replayed clean against {archived.workflow_type}, and it must not: this "
        "fixture is the measured divergence the replay control exists to catch."
    )


async def test_a_divergence_the_sdk_logged_beats_a_replay_that_returned_quietly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control above can lose a race and report a divergent history as clean.

    `replay_failure` races the replay against a watcher on the SDK's own log. `asyncio.wait` with
    `FIRST_COMPLETED` returns *every* future that finished in that cycle, so when the SDK logs
    `TMPRL1100` and the replay coroutine then completes without raising, both are done — and the
    original branch order asked `replay` first, took its silence and returned "". That is
    `test_the_control_still_detects_the_divergence_it_was_built_for` going green for the one reason
    its own docstring says would be a real finding, while the finding is false.

    Found by a full serial run, on a branch that does not touch this file: one failure, passing in
    isolation and under every targeted ordering tried. So it is pinned as *logic* rather than as an
    ordering — an order-dependent reproduction is exactly the kind that rots.

    Driven through the real `replay_failure`, with only the `Replayer` replaced: a stand-in whose
    `replay_workflow` logs the marker and returns cleanly, which is the shape that produced the
    false clean. Both arms, because the fix must not turn every clean replay into a divergence.
    """

    class _Replayer:
        """Stands in for the SDK's `Replayer`: logs a divergence, then finishes without raising."""

        def __init__(self, **_: object) -> None:
            """Accept and ignore whatever `replay_failure` constructs it with."""

        async def replay_workflow(self, _history: object) -> None:
            """Log the marker the watcher is armed for, then return quietly."""
            logging.getLogger("temporalio.worker._workflow").debug(
                "TMPRL1100 workflow task evicted: nondeterminism"
            )

    class _SilentReplayer(_Replayer):
        """The same, logging nothing — a genuinely clean replay."""

        async def replay_workflow(self, _history: object) -> None:
            """Return without logging anything the watcher would trip on."""

    archived = superseded_histories()[0]

    monkeypatch.setattr("temporalio.worker.Replayer", _Replayer)
    diverged = await replay_failure(archived, _workflow_for(archived))
    assert "TMPRL1100" in diverged, (
        "a logged non-determinism must outrank a replay that returned without raising — "
        "otherwise the negative control reports a known-divergent history as clean, "
        f"got {diverged!r}"
    )

    monkeypatch.setattr("temporalio.worker.Replayer", _SilentReplayer)
    assert await replay_failure(archived, _workflow_for(archived)) == "", (
        "with nothing logged, a replay that returns without raising is still clean"
    )


def test_the_background_workflows_this_control_does_not_cover_are_named() -> None:
    """The gap is declared, so adding a workflow forces a decision about its history.

    Covering all of them would mean recording one history per name below against infrastructure
    this repository does not have offline, which is a backlog item rather than a thing to fake.
    (A count is not written here. The two that were said "twenty" and "twenty-one" over a set of
    twenty-two, one of them wrong on the day it was written — which is the argument
    `D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` makes, inside the file whose whole
    subject is a declaration going stale.) What
    must not happen is the gap disappearing from view: "there is a replay check" is exactly the
    kind of sentence this repository has been wrong about before.
    """
    covered = {archived.workflow_type for archived in archived_histories()}
    uncovered = set(_background_workflows()) - covered
    assert uncovered == set(UNCOVERED_BACKGROUND_WORKFLOWS), (
        "the set of background workflows with no archived history has changed: "
        f"newly uncovered {sorted(uncovered - UNCOVERED_BACKGROUND_WORKFLOWS)}, "
        f"now covered {sorted(UNCOVERED_BACKGROUND_WORKFLOWS - uncovered)}. Record a history with "
        "`uv run python tests/recorded_workflow_histories.py`, or add the name to "
        "`UNCOVERED_BACKGROUND_WORKFLOWS`."
    )
