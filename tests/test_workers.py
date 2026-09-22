"""The worker entrypoints register consistent, complete workflow/activity sets.

The worker mains have no other tests. This guards their registration wiring: the modules import
cleanly, their lists have no duplicate registrations, and each worker registers the workflows and
activities it is responsible for — so adding a workflow without registering its activity (or vice
versa) is caught here rather than at runtime on a live queue.

Core has one worker now: every expensive workflow is a declared connector job on its own bundle's
queue as of D-118 — so the bundle case below is not an extra, it is where the heavy work moved to.
"""

from collections.abc import Iterable
from pathlib import Path

from chemclaw.core.config import settings
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


#: What a cached workflow costs a worker, measured 2026-09-22 against the broker `make up` runs.
#:
#: A worker with N workflows parked in `wait_condition`, RSS read from `/proc/self/status` against
#: an idle baseline of 65.8 MiB: 50 -> 137 KiB each, 100 -> 114, 250 -> 82, 500 -> 73, 1,000 -> 71,
#: converging as the fixed cost amortises. Scaling the workflow's own state at 200 cached: 16 KiB
#: -> 91 KiB each, 64 -> 142, 256 -> 347. So the model below: a fixed overhead plus rather more
#: than the state itself, the excess being the replay history the cache keeps beside it.
_CACHED_WORKFLOW_OVERHEAD_KIB = 75
_CACHED_WORKFLOW_STATE_MULTIPLIER = 1.35

#: The per-workflow state this bound is checked against. Not a measurement of this system's
#: workflows — nobody has made one — but the size at which the shipped cache would take a *third*
#: of the worker's memory request, which is the point where the inequality is worth asserting.
_STATE_THE_BOUND_IS_ASSERTED_AT_KIB = 256


def test_the_workflow_cache_fits_the_memory_the_chart_asks_for() -> None:
    """The cache ceiling is a memory bound, so it is held against the chart rather than restated.

    **`max_concurrent_activities` does not bound this and nothing else did.** A workflow-task slot
    is held only while a workflow is being advanced; `max_cached_workflows` is what keeps a started
    workflow *resident between its tasks*, and until 2026-09-22 it was whatever the SDK picked
    (1,000). A child workflow is not an activity, so the activity ceiling never reached the bundle
    children core starts either — which is the `BACKLOG.md` row this closes.

    The inequality rather than a number, on the
    `D-2026-09-18-a-second-process-in-the-pod-is-memory-the-chart-never-declared` model: raising
    the ceiling, shrinking the chart's request, or a workflow that carries more state each move the
    same comparison, and only one of the three is a config edit somebody would think to check.
    """
    import yaml

    chart = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "deploy/helm/chemclaw/values.yaml").read_text()
    )
    request = chart["resources"]["worker"]["requests"]["memory"]
    assert request.endswith("Gi"), f"the worker's memory request is now {request}; re-read this"
    request_kib = int(request[:-2]) * 1024 * 1024

    each = (
        _CACHED_WORKFLOW_OVERHEAD_KIB
        + _CACHED_WORKFLOW_STATE_MULTIPLIER * _STATE_THE_BOUND_IS_ASSERTED_AT_KIB
    )
    cache = settings.worker_max_cached_workflows * each
    assert cache <= request_kib / 2, (
        f"{settings.worker_max_cached_workflows} cached workflows at {each:.0f} KiB each is "
        f"{cache / 1024:.0f} MiB against a worker request of {request} — more than half of it "
        "before the worker has done anything else. Lower worker_max_cached_workflows, or raise "
        "resources.worker.requests.memory in the chart, and say which in the commit message"
    )


def test_the_worker_arms_the_cache_ceiling_it_declares() -> None:
    """A setting nothing passes to the SDK is a number in a file.

    Read off the constructor call rather than by starting a worker, because the shape this guards
    is "somebody added the setting and not the argument" — which a running worker would not show.
    """
    import ast

    source = (
        Path(__file__).resolve().parents[1] / "src/chemclaw/durable/background_worker.py"
    ).read_text()
    passed = {
        keyword.arg
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Worker"
        for keyword in node.keywords
    }
    assert "max_cached_workflows" in passed, (
        "`worker_max_cached_workflows` is declared and not passed to `Worker`, so the SDK's own "
        "default is still the ceiling this repository thinks it chose"
    )
    assert "max_concurrent_activities" in passed
