"""Durable capabilities declare their own queue, and the workers serve what is declared.

A workflow missing from its worker never runs, and nothing fails until one is submitted and waits
in the queue forever.
"""

import asyncio
import contextlib
import json
import subprocess
import sys
import textwrap
import uuid
from typing import Any

import pytest
from temporalio import workflow

# This module defines two workflows (the stance probes at the foot of the file), so Temporal's
# sandbox re-imports it to validate them; passing the first-party import through keeps that
# re-import from walking the whole package inside the sandbox.
with workflow.unsafe.imports_passed_through():
    from chemclaw.durable.registry import (
        describe,
        durable_workflow,
        registered_activities,
        registered_workflows,
    )


def test_every_declared_capability_reaches_its_worker() -> None:
    """The worker serves exactly what the registry holds for its queue.

    Importing the worker module is what registers its capabilities, so this also proves the imports
    are still there.
    """
    from chemclaw.durable.background_worker import BACKGROUND_ACTIVITIES, BACKGROUND_WORKFLOWS

    assert BACKGROUND_WORKFLOWS == registered_workflows("background")
    assert BACKGROUND_ACTIVITIES == registered_activities("background")


def test_the_queues_do_not_overlap() -> None:
    """A capability belongs to one queue. Two would mean two workers racing for it.

    Checked as core's queue against each bundle's: a bundle module that wrote `"background"` instead
    of `bundle_queue` would ask core's worker to serve its heavy closure.
    """
    import chemclaw.connectors.bo.worker
    import chemclaw.connectors.calc.worker  # noqa: F401 — registration
    from chemclaw.connectors.queues import bundle_queue
    from chemclaw.connectors.registry import discovered
    from chemclaw.durable import background_worker  # noqa: F401 — registration

    for kind in (registered_workflows, registered_activities):
        background = {item.__name__ for item in kind("background")}
        for name in discovered():
            bundle = {item.__name__ for item in kind(bundle_queue(name))}
            assert bundle.isdisjoint(background), name


def test_a_connectors_durable_work_is_on_its_own_queue_only() -> None:
    """A bundle's workflows run on the bundle's own worker — the point of the seam."""
    from chemclaw.connectors.bo.activities import propose_next
    from chemclaw.connectors.bo.workflows import BoCampaignWorkflow
    from chemclaw.connectors.calc.activities import run_xtb_calculation
    from chemclaw.connectors.calc.workflows import CalcJobWorkflow
    from chemclaw.connectors.queues import bundle_queue

    for workflow_cls, activity, bundle in (
        (CalcJobWorkflow, run_xtb_calculation, "calc"),
        (BoCampaignWorkflow, propose_next, "bo"),
    ):
        own = bundle_queue(bundle)
        assert workflow_cls in registered_workflows(own)
        assert activity in registered_activities(own)
        assert workflow_cls not in registered_workflows("background")
        assert activity not in registered_activities("background")


def test_cores_workers_import_no_bundle() -> None:
    """Core's workers import no bundle, so a bundle's heavy deps never load into them (D-118).

    The registry is populated at import time, so the import boundary is what keeps a bundle's
    closure out of core. Asserted in a fresh interpreter: importing core's workers pulls in no
    bundle package and none of the heavy third-party libraries that arrive only through one.
    """
    probe = textwrap.dedent(
        """
        import json, sys
        import chemclaw.durable.background_worker  # noqa: F401
        print(json.dumps(sorted(sys.modules)))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    loaded = set(json.loads(completed.stdout.strip().splitlines()[-1]))

    # A *bundle* is a discovered sub-package of `connectors/`; the flat modules beside them are
    # core's own seam and fine to import. Derived from the filesystem, so a new bundle is covered at
    # once.
    from chemclaw.connectors.registry import discovered

    bundle_prefixes = tuple(f"chemclaw.connectors.{name}." for name in discovered())
    bundle_modules = tuple(f"chemclaw.connectors.{name}" for name in discovered())
    offenders = sorted(n for n in loaded if n.startswith(bundle_prefixes) or n in bundle_modules)
    assert not offenders, f"core's workers import bundle module(s): {offenders}"

    heavy = sorted(n for n in loaded if n.split(".")[0] in {"tblite", "bofire", "botorch"})
    assert not heavy, f"core's worker loaded a bundle-only dependency: {heavy}"


#: The queue the registry probes below register on. Not `background`: the registry is
#: process-global, so a probe there would leak into later tests, including the sandbox check.
_PROBE_QUEUE = "registry-probe"


def _probe(name: str, module: str) -> type:
    """A stand-in for a decorated workflow class; the registry reads only these two."""
    return type(name, (), {"__module__": module, "__doc__": "probe"})


def test_a_name_claimed_by_two_modules_is_rejected() -> None:
    """Two definitions sharing a Temporal name means the worker silently drops one."""
    first = _probe("RegistryCollisionProbe", "chemclaw.durable.probe_one")
    durable_workflow(_PROBE_QUEUE)(first)
    with pytest.raises(ValueError, match="claimed by both"):
        durable_workflow(_PROBE_QUEUE)(
            _probe("RegistryCollisionProbe", "chemclaw.durable.probe_two")
        )
    assert first in registered_workflows(_PROBE_QUEUE)


def test_re_registering_the_same_definition_is_allowed() -> None:
    """Temporal's workflow sandbox re-imports workflow modules, re-running the decorator.

    So the guard compares the defining module rather than object identity; otherwise every workflow
    task would raise on a false duplicate.
    """
    module = "chemclaw.durable.reimport_probe"
    durable_workflow(_PROBE_QUEUE)(_probe("RegistryReimportProbe", module))
    durable_workflow(_PROBE_QUEUE)(_probe("RegistryReimportProbe", module))  # must not raise
    names = [item.__name__ for item in registered_workflows(_PROBE_QUEUE)]
    assert names.count("RegistryReimportProbe") == 1


def test_describe_names_what_a_worker_serves() -> None:
    """The startup log line is derived, not restated — so it cannot go stale."""
    import chemclaw.connectors.calc.worker  # noqa: F401 — registration
    from chemclaw.connectors.queues import bundle_queue

    line = describe(bundle_queue("calc"))
    assert "workflows=[CalcJobWorkflow]" in line
    assert "run_xtb_calculation" in line


def _every_served_workflow() -> list[tuple[str, type]]:
    """Every workflow every shipped worker serves, as `(queue, class)`, derived from the imports.

    Imports core's worker and each bundle's `worker` module, as the processes do, so a new bundle
    worker is covered the day it exists.
    """
    import importlib
    import importlib.util

    import chemclaw.durable.background_worker  # noqa: F401 — registration
    from chemclaw.connectors.queues import bundle_queue
    from chemclaw.connectors.registry import discovered

    queues = ["background"]
    for bundle in sorted(discovered()):
        module = f"chemclaw.connectors.{bundle}.worker"
        if importlib.util.find_spec(module) is not None:
            importlib.import_module(module)
            queues.append(bundle_queue(bundle))
    return [(queue, cls) for queue in queues for cls in registered_workflows(queue)]


def test_every_served_workflow_passes_the_sandbox_its_worker_validates_it_in() -> None:
    """A workflow module whose import graph trips the sandbox takes its whole worker down at boot.

    `Worker(...)` validates every workflow in Temporal's import sandbox before polling, and one
    refusal raises out of the constructor, so one import outside
    `workflow.unsafe.imports_passed_through()` stops every job on that queue. This runs the same
    validation (`SandboxedWorkflowRunner.prepare_workflow`) with no broker, inside an event loop
    because validation instantiates the workflow.
    """
    from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

    served = _every_served_workflow()
    assert served, "no worker registered a workflow — the imports above registered nothing"

    async def validate() -> list[str]:
        runner = SandboxedWorkflowRunner()
        refused = []
        for queue, cls in served:
            definition = workflow._Definition.must_from_class(cls)
            try:
                runner.prepare_workflow(definition)
            # Every refusal is collected rather than the first, so one run names them all.
            except Exception as exc:
                cause = exc.__cause__ or exc
                refused.append(f"{queue}/{definition.name}: {type(cause).__name__}: {cause}")
        return refused

    refused = asyncio.run(validate())
    assert not refused, (
        "these workflows fail Temporal's sandbox validation, so the worker serving them refuses to "
        "start at all — pass the offending first-party import through "
        "`workflow.unsafe.imports_passed_through()`:\n" + "\n".join(refused)
    )


def test_every_workflow_on_the_job_path_can_actually_fail() -> None:
    """A plain exception in workflow code must fail the run, not park it forever.

    The SDK treats such an exception as a suspected bug and retries the workflow task forever,
    ignoring the retry policy. On the job path a chemist is waiting and these runs carry no
    `execution_timeout`, so they must declare failure. Periodic workflows are decided individually
    in `test_every_background_workflow_holds_the_stance_argued_for_it`. Checked over the registry,
    so a bundle added later is covered.
    """
    import chemclaw.connectors.bo.workflows
    import chemclaw.connectors.calc.workflows  # noqa: F401 — registration
    from chemclaw.connectors.queues import bundle_queue
    from chemclaw.connectors.registry import discovered
    from chemclaw.durable.connector_job import ConnectorJobWorkflow

    on_the_job_path: list[type] = [ConnectorJobWorkflow]
    for name in discovered():
        on_the_job_path.extend(registered_workflows(bundle_queue(name)))

    undeclared = [
        cls.__name__
        for cls in on_the_job_path
        if not getattr(
            getattr(cls, "__temporal_workflow_definition", None), "failure_exception_types", ()
        )
    ]
    assert not undeclared, (
        f"{undeclared} raise plain exceptions into an unbounded workflow-task-failure loop instead "
        "of failing: add `@workflow.defn(failure_exception_types=[Exception])`. A job that hangs "
        "while the chemist is told it is running is the failure this prevents."
    )


# The stance argued for each workflow on core's `background` queue, from
# `D-2026-08-27-a-periodic-job-decides-for-itself-whether-a-bug-should-park-it`.
#
# **The rule**: a plain exception in workflow code parks the run indefinitely. Declare
# `failure_exception_types` where that park has no ceiling or a ceiling somebody waits through — a
# workflow started by a tool, CLI or webhook, or a fan-out child a chemist's parent is polling.
# Leave it parking where the only starter is a Temporal Schedule: the run is bounded by
# `schedule_run_timeout_seconds`, nothing reads its result, and the work is cursored or idempotent.
# `EvalDriftWorkflow` is the argued exception on the schedule side — see its decorator.
_MUST_FAIL = frozenset(
    {
        # The job path. Named here as well as derived above, so this table is a complete account of
        # the queue rather than a partial one that reads as complete.
        "ConnectorJobWorkflow",
        "TemplateWorkflow",
        # Started by an agent tool for a named chemist, with no `execution_timeout`, and polled
        # through `get_durable_job_status` — the job path in everything but its queue.
        "CampaignSynthesisWorkflow",
        "PlaybookDistillationWorkflow",
        "OptimizationCampaignWorkflow",
        "ObservationPromotionWorkflow",
        "DevelopmentReportWorkflow",
        # Started by `rank_competing_hypotheses` for a named chemist and polled through
        # `get_durable_job_status`, so it is on the job path: a parked tournament is a job the
        # chemist is waiting on that never answers and never fails.
        "HypothesisTournamentWorkflow",
        # The durable wait. A parked wait is a request left in an inbox with nothing listening,
        # indistinguishable from one nobody has got to yet, so a bug must fail the wait.
        "AwaitAnswerWorkflow",
        # Fan-out children of those. A parked child is dropped only when its execution timeout
        # expires, and that hour is spent by the parent the chemist is polling.
        "PublishNoteWorkflow",
        "ReportSectionWorkflow",
        # Schedule-only, but declared: a failed run reaches an operator through
        # `ScheduleHealth.last_outcome`, while a parked one reaches nobody as the index goes stale.
        "NoteReindexWorkflow",
        # The check-in over blocked work, the one nightly sweep that fails rather than parks: a
        # parked run reads as "nothing of yours is blocked", and under `SKIP` every later night is
        # silently skipped.
        "CheckInWorkflow",
        # An uncapped second starter beside the Schedule: the live lane's backfill, which awaits
        # `handle.result()`.
        "ElnSyncWorkflow",
        # Schedule-only, and declared anyway: its output is a claim about a moment, so a resumed
        # park delivers a day-old verdict as current.
        "EvalDriftWorkflow",
        # Schedule-only drains declared by their own modules (`corpus_sync.py`, `label_sync.py`).
        # The rule would not require it; removing one is a decision, not a tidy-up.
        "ReactionCorpusWorkflow",
        "ReactionLabelWorkflow",
    }
)

# Deliberately parking. Each is reached only from a Temporal Schedule, so the park is bounded by
# `schedule_run_timeout_seconds`; nothing reads the run; the work is idempotent or cursored, so a
# skipped fire costs a delay, and a parked run can still finish once a fix is deployed.
_MAY_PARK = frozenset(
    {
        "ArtifactEvictionWorkflow",
        "DigestWorkflow",
        "DocumentShareSyncWorkflow",
        "ObservationSynthesisWorkflow",
        "PublishResultsWorkflow",
        # The commitment mirror. Parks rather than fails: a stale mirror reports its own staleness
        # through `observed_at`, so the failure is visible without the workflow failing.
        "CommitmentSyncWorkflow",
        # The orphaned-wait sweep: Schedule-only, idempotent, and nothing reads the run
        # (`durable/orphaned_waits.py`).
        "OrphanedWaitsWorkflow",
        "RetentionWorkflow",
        # Artefact push expiry: Schedule-only, idempotent and nothing reads the run — its rows are
        # notifications whose source of truth is the artefact list, so a parked run costs stale
        # mailbox rows until a fix ships, never a lost answer (`durable/retention.py`).
        "ExhibitPushPruneWorkflow",
    }
)


def _real_background_workflows() -> dict[str, type]:
    """The `@workflow.defn` classes on core's queue, by name.

    Filtered on `__temporal_workflow_definition`, because tests above register bare `type()` probes
    on `background`; it is the same fact the stance check reads.
    """
    return {
        cls.__name__: cls
        for cls in registered_workflows("background")
        if getattr(cls, "__temporal_workflow_definition", None) is not None
    }


def _declares_failure(cls: type) -> bool:
    """Whether `cls` turns a plain `Exception` in workflow code into a workflow *failure*.

    Read via `workflow_is_failure_exception`, checking `Exception` itself is covered —
    `failure_exception_types=[ValueError]` is a different decision.
    """
    definition = getattr(cls, "__temporal_workflow_definition", None)
    declared = getattr(definition, "failure_exception_types", ()) or ()
    return any(issubclass(Exception, declared_type) for declared_type in declared)


def test_every_background_workflow_holds_the_stance_argued_for_it() -> None:
    """Each periodic workflow either fails or parks *because someone argued it should*.

    Both directions are asserted: a `_MUST_FAIL` workflow that stops declaring goes red, and so does
    a `_MAY_PARK` workflow that starts. Flipping a stance means moving the name, a diff a reviewer
    sees. `test_the_two_stances_behave_as_the_table_assumes` checks what the stances actually do.
    """
    from chemclaw.durable import background_worker  # noqa: F401 — registration

    workflows = _real_background_workflows()
    registered = set(workflows)
    assert not (_MUST_FAIL & _MAY_PARK), "a workflow cannot hold two stances at once"

    undecided = sorted(registered - _MUST_FAIL - _MAY_PARK)
    assert not undecided, (
        f"{undecided} is on the background queue with no stance recorded. Decide whether a plain "
        "exception should fail it or park it — the rule and the worked cases are in "
        "D-2026-08-27-a-periodic-job-decides-for-itself-whether-a-bug-should-park-it — then add "
        "the name to _MUST_FAIL or _MAY_PARK and say why beside its decorator."
    )
    departed = sorted((_MUST_FAIL | _MAY_PARK) - registered)
    assert not departed, f"{departed} no longer exists; drop the stale row from this table"

    should_fail = sorted(
        name for name in _MUST_FAIL & registered if not _declares_failure(workflows[name])
    )
    assert not should_fail, (
        f"{should_fail} was argued to fail rather than park and no longer declares "
        "`failure_exception_types=[Exception]`. Restore it, or move the name to _MAY_PARK with the "
        "argument for the change in a new ADR."
    )
    should_park = sorted(
        name for name in _MAY_PARK & registered if _declares_failure(workflows[name])
    )
    assert not should_park, (
        f"{should_park} was argued to keep parking — nothing reads it, its only starter is a "
        "Schedule that already bounds the run, and its work is idempotent — and now declares "
        "`failure_exception_types`. That is a stance change: argue it in a new ADR and move the "
        "name, rather than sweeping the queue."
    )


# Two probes for the measurement below. Module-level because Temporal's workflow sandbox re-imports
# the defining module, and deliberately **not** registered with `durable_workflow` — they are not
# capabilities, they are the two spellings under test.
@workflow.defn
class _ParksOnAPlainException:
    """A workflow with no declaration: the SDK default the periodic jobs mostly keep."""

    @workflow.run
    async def run(self) -> str:
        """Raise the shape of a redeploy bug — a plain exception in workflow code."""
        raise ValueError("a redeploy bug, raised in workflow code")


@workflow.defn(failure_exception_types=[Exception])
class _FailsOnAPlainException:
    """The same workflow with the declaration `_MUST_FAIL` names."""

    @workflow.run
    async def run(self) -> str:
        """Raise the same exception, so the stance is the only difference between the two."""
        raise ValueError("a redeploy bug, raised in workflow code")


def test_the_two_stances_behave_as_the_table_assumes() -> None:
    """One plain exception, two decorators, against a real broker: one fails, one hangs.

    The decision table rests on the SDK parking an exception in workflow code unless declared
    otherwise, so the claim is run on the time-skipping server: the declared workflow reaches
    `FAILED`, the undeclared one is still `RUNNING` with no `execution_timeout` to end it.
    """
    from temporalio.client import WorkflowFailureError
    from temporalio.types import MethodAsyncNoParam
    from temporalio.worker import Worker

    from tests.temporal_env import start_env_or_skip

    # Typed as the no-argument workflow method Temporal's own `start_workflow` overload takes, for
    # the reason `agent/durable_tools.py` gives beside its own such mapping: a bare list of two
    # unrelated workflow classes degrades to `type[object]` and `.run` stops type-checking.
    starts: dict[str, MethodAsyncNoParam[Any, str]] = {
        "_FailsOnAPlainException": _FailsOnAPlainException.run,
        "_ParksOnAPlainException": _ParksOnAPlainException.run,
    }

    async def measure() -> dict[str, str]:
        outcomes: dict[str, str] = {}
        async with await start_env_or_skip() as env:
            probes = [_FailsOnAPlainException, _ParksOnAPlainException]
            async with Worker(env.client, task_queue="stance-probe", workflows=probes):
                for name, start in starts.items():
                    handle = await env.client.start_workflow(
                        start, id=f"{name}-{uuid.uuid4()}", task_queue="stance-probe"
                    )
                    # Both terminal states are legitimate answers here; the assertion is on the
                    # status the server ends up reporting, not on how the wait ended.
                    with contextlib.suppress(WorkflowFailureError, TimeoutError):
                        await asyncio.wait_for(handle.result(), timeout=5)
                    status = (await handle.describe()).status
                    outcomes[name] = status.name if status is not None else "UNREPORTED"
        return outcomes

    outcomes = asyncio.run(measure())
    assert outcomes["_FailsOnAPlainException"] == "FAILED"
    assert outcomes["_ParksOnAPlainException"] == "RUNNING", (
        "the undeclared workflow completed or failed on its own — if the SDK has changed this "
        "default, the trade D-2026-08-27 decided per workflow no longer exists and the table above "
        "should be retired rather than maintained"
    )
