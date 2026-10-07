"""Today's workflow code still accepts the histories the shipped code wrote.

A closed history records every command the shipped code emitted, so replaying it detects a
divergence anywhere in the sequence — including the early positions an unfinished run replays
when the background worker is replaced. It is a conservative proxy: red is a question about where
the sequence diverges, not automatically an outage. `tests/recorded_workflow_histories.py` holds
the fixtures and the rule for re-recording one.
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

# The workflows on core's `background-jobs` queue with no archived history, and so no guard against
# a redeploy that changes their command sequence. Declared so the gap is visible: adding a workflow
# to core's queue means recording a fixture for it or adding its name here.
#
# Only `background-jobs`, because its worker deploys `Recreate` and the new generation inherits
# every unfinished run; connector workers use a rolling update.
UNCOVERED_BACKGROUND_WORKFLOWS = frozenset(
    {
        "ArtifactEvictionWorkflow",
        "AwaitAnswerWorkflow",
        "CampaignSynthesisWorkflow",
        "CommitmentSyncWorkflow",
        # New in `D-2026-09-25-a-wait-nobody-can-settle-is-settled-by-a-sweep`. It earns a fixture
        # at the first change to its command sequence, the way the two above did.
        "OrphanedWaitsWorkflow",
        # New in `D-2026-10-03-an-artefact-push-expires-on-its-own-schedule`; earns a fixture at the
        # first change to its command sequence, as `OrphanedWaitsWorkflow` does.
        "ExhibitPushPruneWorkflow",
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

    A history from a bundle's workflow fails with the reason rather than a `KeyError`: this control
    is scoped to core's queue.
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

    Red means the change alters the workflow's command sequence. The honest responses are in
    `recorded_workflow_histories`' docstring; silently re-recording the fixture is not one of them.
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

    Proves the replay is not silently doing nothing, using a `TemplateWorkflow` history from before
    `record_job` was dispatched. If this goes green, the `_record_run` dispatches are gone or gated
    — a real finding, not a fixture to delete.
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
    """A divergence the SDK logged beats a replay that returned quietly.

    `replay_failure` races the replay against a watcher on the SDK's log; `asyncio.wait` can return
    both as done, and the logged `TMPRL1100` must win. Pinned as logic with a stand-in `Replayer`
    that logs the marker and returns cleanly, plus the clean arm, so a clean replay is still clean.
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

    Recording a history per name needs infrastructure not available offline; what must not happen is
    the gap disappearing from view.
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
