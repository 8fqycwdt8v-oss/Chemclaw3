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


def test_the_calc_connectors_worker_serves_every_expensive_xtb_task() -> None:
    """One durable job on the bundle's own queue serves all five expensive calculations.

    A CREST search or a multi-species reaction is minutes of saturated CPU, which is why it
    is durable at all (D-006). What changed is *whose* worker runs it: `connector-calc`, so
    the queue can be sized for this capability alone and core's workers never load `tblite`
    or the `xtb`/`crest` binaries. The five tasks share one workflow — `XtbJobSpec` is a
    closed union discriminated on `kind` — so the queue choice is made once, not per
    capability.
    """
    import connectors.calc.worker  # noqa: F401 — importing it is what registers the bundle
    from connectors.calc.workflows import CalcJobWorkflow
    from connectors.queues import bundle_queue
    from connectors.registry import discovered
    from workflows.registry import registered_activities, registered_workflows

    # Read from the registry, not from module constants. `CALC_WORKFLOWS`/`CALC_ACTIVITIES`/
    # `TASK_QUEUE` were hand-maintained lists that could silently disagree with what the bundle's
    # modules actually define — the failure `workflows.registry` exists to prevent, re-created one
    # level down (D-118). There is nothing left to disagree with.
    queue = bundle_queue("calc")
    assert CalcJobWorkflow in registered_workflows(queue)
    assert registered_activities(queue)  # a workflow with no activity is a wiring bug
    # The queue is derived from the bundle name, so manifest and worker cannot drift; what is
    # still worth asserting is that every job routes to the one workflow this bundle serves.
    _, manifest = discovered()["calc"]
    jobs = manifest.jobs
    assert jobs and {job.task_queue for job in jobs} == {queue}
    assert {job.workflow for job in jobs} == {"CalcJobWorkflow"}


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
