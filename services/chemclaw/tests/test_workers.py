"""The worker entrypoints register consistent, complete workflow/activity sets.

The worker mains have no other tests. This guards their registration wiring: both modules
import cleanly, their lists have no duplicate registrations, and each worker registers the
workflows and activities it is responsible for — so adding a workflow without registering its
activity (or vice versa) is caught here rather than at runtime on a live queue.
"""

from collections.abc import Iterable

from workers.background_worker import BACKGROUND_ACTIVITIES, BACKGROUND_WORKFLOWS
from workers.hpc_worker import HPC_ACTIVITIES, HPC_WORKFLOWS
from workflows.eln_sync import ElnSyncWorkflow, load_sync_cursor, store_sync_cursor
from workflows.qm_job import QMJobWorkflow


def _names(items: Iterable[object]) -> list[str]:
    return [getattr(item, "__name__", repr(item)) for item in items]


def test_hpc_worker_registers_qm_workflow_and_activities() -> None:
    """The HPC worker serves the QM workflow and all four QM activities."""
    assert QMJobWorkflow in HPC_WORKFLOWS
    assert {"prepare_input", "submit_to_hpc", "poll_hpc_status", "parse_qm_output"} <= set(
        _names(HPC_ACTIVITIES)
    )


def test_background_worker_registers_eln_sync_with_cursor_activities() -> None:
    """The ELN sync workflow and its self-cursoring activities are all registered."""
    assert ElnSyncWorkflow in BACKGROUND_WORKFLOWS
    for activity in (load_sync_cursor, store_sync_cursor):
        assert activity in BACKGROUND_ACTIVITIES


def test_registration_lists_have_no_duplicates() -> None:
    """No workflow or activity is registered twice on either worker (wiring-drift guard)."""
    for workflows in (HPC_WORKFLOWS, BACKGROUND_WORKFLOWS):
        assert len(workflows) == len(set(workflows))
    for activities in (HPC_ACTIVITIES, BACKGROUND_ACTIVITIES):
        names = _names(activities)
        assert len(names) == len(set(names))


def test_worker_registration_lists_are_non_empty() -> None:
    """Both workers register at least one workflow and one activity."""
    assert HPC_WORKFLOWS and HPC_ACTIVITIES
    assert BACKGROUND_WORKFLOWS and BACKGROUND_ACTIVITIES
