"""The worker entrypoints register consistent, complete workflow/activity sets.

The worker mains have no other tests. This guards their registration wiring: the modules import
cleanly, their lists have no duplicate registrations, and each worker registers the workflows and
activities it is responsible for — so adding a workflow without registering its activity (or vice
versa) is caught here rather than at runtime on a live queue.

Core has one worker now: every expensive workflow is a declared connector job on its own bundle's
queue as of D-118 — so the bundle case below is not an extra, it is where the heavy work moved to.
"""

from collections.abc import Iterable

from chemclaw.durable.background_worker import BACKGROUND_ACTIVITIES, BACKGROUND_WORKFLOWS
from chemclaw.durable.eln_sync import ElnSyncWorkflow, load_sync_cursor, store_sync_cursor


def _names(items: Iterable[object]) -> list[str]:
    return [getattr(item, "__name__", repr(item)) for item in items]


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
    import chemclaw.connectors.calc.worker  # noqa: F401 — importing it is what registers the bundle
    from chemclaw.connectors.calc.workflows import CalcJobWorkflow
    from chemclaw.connectors.queues import bundle_queue
    from chemclaw.connectors.registry import discovered
    from chemclaw.durable.registry import registered_activities, registered_workflows

    # Read from the registry, not from module constants. `CALC_WORKFLOWS`/`CALC_ACTIVITIES`/
    # `TASK_QUEUE` were hand-maintained lists that could silently disagree with what the bundle's
    # modules actually define — the failure `workflows.registry` exists to prevent, re-created one
    # level down (D-118). There is nothing left to disagree with.
    queue = bundle_queue("calc")
    assert CalcJobWorkflow in registered_workflows(queue)
    assert registered_activities(queue)  # a workflow with no activity is a wiring bug
    # The queue is derived from the bundle name at dispatch and declared nowhere (D-150), so
    # manifest and worker have nothing left to disagree about — `tests/test_connector_jobs.py`
    # pins the derived value on the launch payload. What is still worth asserting here is that
    # every job routes to the one workflow this bundle serves.
    _, manifest = discovered()["calc"]
    jobs = manifest.jobs
    assert jobs and {job.workflow for job in jobs} == {"CalcJobWorkflow"}


def test_registration_lists_have_no_duplicates() -> None:
    """No workflow or activity is registered twice on the worker (wiring-drift guard)."""
    assert len(BACKGROUND_WORKFLOWS) == len(set(BACKGROUND_WORKFLOWS))
    names = _names(BACKGROUND_ACTIVITIES)
    assert len(names) == len(set(names))


def test_worker_registration_lists_are_non_empty() -> None:
    """The worker registers at least one workflow and one activity."""
    assert BACKGROUND_WORKFLOWS and BACKGROUND_ACTIVITIES
