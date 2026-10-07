"""The worker entrypoints register consistent, complete workflow/activity sets.

The modules import cleanly, their lists have no duplicates, and each worker registers the
workflows and activities it owns — so a workflow registered without its activity (or vice versa)
is caught here rather than on a live queue. Core runs one worker; expensive workflows are
connector jobs on their bundle's own queue (D-118).
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

    They run on `connector-calc`, so the queue is sized for this capability alone and core's workers
    never load the xTB stack. The five tasks share one workflow (`XtbJobSpec` is a union
    discriminated on `kind`), so the queue is chosen once.
    """
    import chemclaw.connectors.calc.worker  # noqa: F401 — importing it is what registers the bundle
    from chemclaw.connectors.calc.workflows import CalcJobWorkflow
    from chemclaw.connectors.queues import bundle_queue
    from chemclaw.connectors.registry import discovered
    from chemclaw.durable.registry import registered_activities, registered_workflows

    # Read from the registry, not from hand-maintained module constants that could disagree with
    # what the bundle's modules define.
    queue = bundle_queue("calc")
    assert CalcJobWorkflow in registered_workflows(queue)
    assert registered_activities(queue)  # a workflow with no activity is a wiring bug
    # The queue is derived from the bundle name at dispatch (pinned in
    # `tests/test_connector_jobs.py`); what is asserted here is that every job routes to the one
    # workflow this bundle serves.
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


#: What a cached workflow costs a worker, as RSS over an idle baseline, in three terms:
#:
#: - **fixed**, the per-workflow asymptote as the cache fills (~65-75 KiB);
#: - **state**, ~1.05x the workflow's own state;
#: - **history**, an allowance for signal history: real and able to triple a low-state workflow,
#:   but its per-signal coefficient is not established, hence an allowance.
_CACHED_WORKFLOW_OVERHEAD_KIB = 85
_CACHED_WORKFLOW_STATE_MULTIPLIER = 1.05
_CACHED_WORKFLOW_HISTORY_ALLOWANCE_KIB = 175

#: The per-workflow state this bound is checked against — an assumption, not a measurement of this
#: system's workflows. With the history allowance it is the shape of a long-lived campaign parent,
#: the most expensive workflow this repository plausibly parks.
_STATE_THE_BOUND_IS_ASSERTED_AT_KIB = 256

#: The share of the worker's memory request the cache may claim. Half, because the process also runs
#: activities, holds a Postgres pool and an RDKit import graph.
_THE_SHARE_OF_THE_REQUEST_THE_CACHE_MAY_CLAIM = 0.5


def _kibibytes(quantity: str) -> float:
    """A Kubernetes memory quantity in KiB.

    Parses every legal suffix and fractional values (`1Gi`, `1024Mi`, `0.5Gi`), so an equivalent
    edit to the chart does not red the test.
    """
    for suffix, factor in (("Gi", 1024 * 1024), ("Mi", 1024), ("Ki", 1)):
        if quantity.endswith(suffix):
            return float(quantity[: -len(suffix)]) * factor
    raise AssertionError(
        f"the worker's memory request is {quantity!r}, in a unit this test cannot read. Add it "
        "above rather than changing the request to suit the test"
    )


def test_the_workflow_cache_fits_the_memory_the_chart_asks_for() -> None:
    """The cache ceiling is a memory bound, so it is held against the chart rather than restated.

    `max_cached_workflows` keeps started workflows resident between tasks, and the activity ceiling
    does not reach child workflows, so nothing else bounds this memory. Asserted as an inequality,
    so raising the ceiling, shrinking the request or growing workflow state all red it. The SDK
    default of 1,000 does not fit a 1Gi request at the asserted shape, hence the shipped 750.
    """
    import yaml

    chart = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "deploy/helm/chemclaw/values.yaml").read_text()
    )
    request = chart["resources"]["worker"]["requests"]["memory"]
    request_kib = _kibibytes(request)

    each = (
        _CACHED_WORKFLOW_OVERHEAD_KIB
        + _CACHED_WORKFLOW_STATE_MULTIPLIER * _STATE_THE_BOUND_IS_ASSERTED_AT_KIB
        + _CACHED_WORKFLOW_HISTORY_ALLOWANCE_KIB
    )
    cache = settings.worker_max_cached_workflows * each
    budget = request_kib * _THE_SHARE_OF_THE_REQUEST_THE_CACHE_MAY_CLAIM
    assert cache <= budget, (
        f"{settings.worker_max_cached_workflows} cached workflows at {each:.0f} KiB each is "
        f"{cache / 1024:.0f} MiB against a worker request of {request}, over the "
        f"{budget / 1024:.0f} MiB this cache may claim — before the worker has done anything "
        "else. Lower worker_max_cached_workflows, or raise resources.worker.requests.memory in "
        "the chart, and say which in the commit message"
    )


def test_every_worker_entrypoint_arms_the_cache_ceiling_it_declares() -> None:
    """A setting nothing passes to the SDK is a number in a file.

    Both entrypoints, because bundle children are cached in `connectors/worker.py`'s process. Read
    off the constructor call: it must be inside the entrypoint the deployment runs, and the
    argument's value must be the setting rather than a pasted literal.
    """
    import ast

    root = Path(__file__).resolve().parents[1]
    for module, entrypoint in (
        ("src/chemclaw/durable/background_worker.py", "main"),
        ("src/chemclaw/connectors/worker.py", "run_bundle_worker"),
    ):
        tree = ast.parse((root / module).read_text())
        served = [
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == entrypoint
        ]
        assert served, f"{module} has no module-level `{entrypoint}` for the deployment to run"

        passed = {
            keyword.arg: keyword.value
            for node in ast.walk(served[0])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Worker"
            for keyword in node.keywords
        }
        for argument, field in (
            ("max_cached_workflows", "worker_max_cached_workflows"),
            ("max_concurrent_activities", "worker_max_concurrent_activities"),
        ):
            value = passed.get(argument)
            assert value is not None, (
                f"`{field}` is declared and `{module}` does not pass `{argument}` to `Worker`, so "
                "the SDK's own default is still the ceiling this repository thinks it chose"
            )
            assert ast.unparse(value) == f"settings.{field}", (
                f"`{module}` passes `{argument}={ast.unparse(value)}`, which is not "
                f"`settings.{field}` — so the field a deployment overrides is not the number the "
                "worker arms"
            )
