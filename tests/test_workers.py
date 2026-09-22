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
#: Three terms, because two were not enough. A worker with N workflows parked and RSS read from
#: `/proc/self/status` against an idle baseline of ~66 MiB:
#:
#: - **fixed**, converging as it amortises: 50 cached -> 137 KiB each, 100 -> 114, 250 -> 82,
#:   500 -> 73, 1,000 -> 71. A second run with a timer park rather than a `wait_condition` one
#:   gave 152/114/80/69/64, so the shape does not matter and the asymptote is ~65-75.
#: - **state**, at 200 cached: 16 KiB of state -> +17 KiB over the zero-state figure, 64 -> +66,
#:   256 -> +275. That is ~1.05x, the residual being the 1 KiB strings' own object headers.
#: - **history**, which the first model had no variable for and called "replay history" while
#:   folding it into the state term. It cannot be that: the excess over state is *flat* in state.
#:   Measured on its own, at zero state and 200 cached: no signals -> 69 KiB each, twenty -> 199,
#:   a hundred -> 246. Two independent runs put the slope at 0.95 and at ~1.8 KiB per signal, so
#:   what is established is that the axis is real and can triple a low-state workflow, **not** its
#:   coefficient — which is why the allowance below is named as an allowance.
#:
#: The first model was `75 + 1.35 x state`, and the 1.35 was `347 / 256` — the total per-workflow
#: cost divided by the state, so the fixed overhead was counted inside the quotient and then added
#: again. It overstated a 256 KiB workflow by ~18%, in the safe direction, from this file's own
#: numbers.
_CACHED_WORKFLOW_OVERHEAD_KIB = 85
_CACHED_WORKFLOW_STATE_MULTIPLIER = 1.05
_CACHED_WORKFLOW_HISTORY_ALLOWANCE_KIB = 175

#: The per-workflow state this bound is checked against. Not a measurement of this system's
#: workflows — nobody has made one — and named so the next reader can see that. It pairs with the
#: history allowance above, which is what a hundred signals cost at zero state: together they are
#: the shape of a long-lived campaign parent, which is the most expensive workflow this repository
#: plausibly parks, rather than the average one.
_STATE_THE_BOUND_IS_ASSERTED_AT_KIB = 256

#: The share of the worker's memory request the cache may claim. Half, because the cache is one
#: resident structure in a process that also runs `worker_max_concurrent_activities` activities,
#: holds a Postgres pool and an RDKit-carrying import graph — a cache past half the *request* is
#: over the reservation before the work starts, which is the shape
#: `D-2026-09-18-a-second-process-in-the-pod-is-memory-the-chart-never-declared` names.
_THE_SHARE_OF_THE_REQUEST_THE_CACHE_MAY_CLAIM = 0.5


def _kibibytes(quantity: str) -> float:
    """A Kubernetes memory quantity in KiB.

    `1Gi`, `1024Mi` and `0.5Gi` are all legal and all mean something. Written out rather than
    `endswith("Gi")` because the first version reddened on `1024Mi` — a semantically identical
    edit — and raised a bare `ValueError` on `0.5Gi`. A reader who hits either is debugging a unit
    parser instead of a memory budget.
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

    **`max_concurrent_activities` does not bound this and nothing else did.** A workflow-task slot
    is held only while a workflow is being advanced; `max_cached_workflows` is what keeps a started
    workflow *resident between its tasks*, and until 2026-09-22 it was whatever the SDK picked
    (1,000). A child workflow is not an activity, so the activity ceiling never reached the bundle
    children core starts either — which is the `BACKLOG.md` row this closes.

    The inequality rather than a number, on the
    `D-2026-09-18-a-second-process-in-the-pod-is-memory-the-chart-never-declared` model: raising
    the ceiling, shrinking the chart's request, or a workflow that carries more state each move the
    same comparison, and only one of the three is a config edit somebody would think to check.

    **It is the SDK's 1,000 that does not fit**, which is why the shipped ceiling is 750: at the
    asserted shape 1,000 comes to ~516 MiB against a 1Gi request. The two-term model this file
    first carried put the same 1,000 at ~340 MiB and would have passed.
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

    **Both entrypoints, because the bundle one was the gap.** Core starts a bundle's job as a child
    workflow on `connector-<name>`, so the workflow that carries a CREST search's state is cached
    in `connectors/worker.py`'s process, not in core's — and that call had only the activity
    ceiling on it. Asserting core alone would have passed over the half of the fleet the
    `BACKLOG.md` row is about.

    Read off the constructor call rather than by starting a worker, because the shape this guards
    is "somebody added the setting and not the argument" — which a running worker would not show.
    Three things are checked and the first two are what a name-only assertion missed: the call is
    the one inside the entrypoint the deployment runs (a `Worker(` in a helper nothing calls would
    otherwise satisfy it), and the argument's *value* is the setting rather than a literal somebody
    pasted, which is the whole point of the field existing.
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
