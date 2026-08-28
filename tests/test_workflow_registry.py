"""Durable capabilities declare their own queue, and the workers serve what is declared.

The failure this guards is silent and was hit for real while building the xTB job: a
workflow that is written, tested and imported but missing from a worker's hardcoded list
never runs, and nothing fails until someone submits one and it waits in the queue
forever.
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

# This module *defines* two workflows (the stance probes at the foot of the file), so Temporal's
# sandbox re-imports it — and everything it imports — when it validates them. Passing the
# first-party import through keeps that re-import from walking the whole package inside the
# sandbox's restricted environment, the same guard `tests/test_orchestrator.py` needed for the
# same reason.
with workflow.unsafe.imports_passed_through():
    from chemclaw.durable.registry import (
        describe,
        durable_workflow,
        registered_activities,
        registered_workflows,
    )


def test_every_declared_capability_reaches_its_worker() -> None:
    """The worker serves exactly what the registry holds for its queue.

    Importing the worker module is what registers its capabilities, so this also
    proves the imports are still there — the one thing adding a workflow to a *new*
    module still requires.
    """
    from chemclaw.durable.background_worker import BACKGROUND_ACTIVITIES, BACKGROUND_WORKFLOWS

    assert BACKGROUND_WORKFLOWS == registered_workflows("background")
    assert BACKGROUND_ACTIVITIES == registered_activities("background")


def test_the_queues_do_not_overlap() -> None:
    """A capability belongs to one queue. Two would mean two workers racing for it.

    Core has one queue now, so the pairs worth checking are core's against each bundle's — which is
    where the overlap could actually appear, because a bundle module that forgot `bundle_queue`
    and wrote `"background"` would silently ask core's worker to serve its heavy closure.
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
    """The real guarantee behind "a bundle's heavy deps never load into core's worker" (D-118).

    This used to be asserted as the *absence of a decorator*: bundles left their workflows
    undecorated, on the reasoning that registering them would put `bofire`/`tblite` into core's
    background worker. That reasoning had the mechanism backwards. The registry is populated at
    **import** time, and core's workers never import `connectors.<bundle>` — so a decorator
    cannot move anything into core, and withholding it bought nothing while forcing each bundle
    to hand-maintain the list of what its worker serves.

    What actually keeps the closure out is the import boundary, so that is what this asserts, in
    a fresh interpreter: importing core's workers must not pull in a bundle package or any of the
    heavy third-party libraries that arrive only through one.
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

    # A *bundle* is a discovered sub-package of `connectors/`; the flat modules beside them
    # (`registry`, `queues`, `identity`, `transport`, …) are core's own seam and are fine to
    # import. Derived from the filesystem rather than listed, so adding a bundle extends the
    # check on the day it is created instead of the day someone remembers to widen a set.
    from chemclaw.connectors.registry import discovered

    bundle_prefixes = tuple(f"chemclaw.connectors.{name}." for name in discovered())
    bundle_modules = tuple(f"chemclaw.connectors.{name}" for name in discovered())
    offenders = sorted(n for n in loaded if n.startswith(bundle_prefixes) or n in bundle_modules)
    assert not offenders, f"core's workers import bundle module(s): {offenders}"

    heavy = sorted(n for n in loaded if n.split(".")[0] in {"tblite", "bofire", "botorch"})
    assert not heavy, f"core's worker loaded a bundle-only dependency: {heavy}"


def _probe(name: str, module: str) -> type:
    """A stand-in for a decorated workflow class; the registry reads only these two."""
    return type(name, (), {"__module__": module, "__doc__": "probe"})


def test_a_name_claimed_by_two_modules_is_rejected() -> None:
    """Two definitions sharing a Temporal name means the worker silently drops one."""
    first = _probe("RegistryCollisionProbe", "chemclaw.durable.probe_one")
    durable_workflow("background")(first)
    with pytest.raises(ValueError, match="claimed by both"):
        durable_workflow("background")(
            _probe("RegistryCollisionProbe", "chemclaw.durable.probe_two")
        )
    assert first in registered_workflows("background")


def test_re_registering_the_same_definition_is_allowed() -> None:
    """Temporal's workflow sandbox re-imports workflow modules, re-running the decorator.

    So the guard compares the defining *module* rather than object identity — otherwise
    every workflow task would raise on a duplicate that is not one, and the worker would
    die on its first piece of work.
    """
    module = "chemclaw.durable.reimport_probe"
    durable_workflow("background")(_probe("RegistryReimportProbe", module))
    durable_workflow("background")(_probe("RegistryReimportProbe", module))  # must not raise
    names = [item.__name__ for item in registered_workflows("background")]
    assert names.count("RegistryReimportProbe") == 1


def test_describe_names_what_a_worker_serves() -> None:
    """The startup log line is derived, not restated — so it cannot go stale."""
    import chemclaw.connectors.calc.worker  # noqa: F401 — registration
    from chemclaw.connectors.queues import bundle_queue

    line = describe(bundle_queue("calc"))
    assert "workflows=[CalcJobWorkflow]" in line
    assert "run_xtb_calculation" in line


def test_every_workflow_on_the_job_path_can_actually_fail() -> None:
    """A plain exception in workflow code must fail the run, not park it forever.

    The SDK treats an exception raised in workflow *code* as a suspected bug: it suspends the run
    in an internal workflow-task-failure loop that ignores the retry policy and never gives up.
    That is a defensible default for a workflow nobody is waiting on. It is the wrong one for this
    path, where a chemist has already been told the job is running and the only way they ever hear
    otherwise is a push-back the run must reach in order to send.

    Both halves were measured against a live broker before this test existed. A bundle workflow
    returning something that is not the envelope left `ConnectorJobWorkflow` RUNNING indefinitely,
    its history repeating `workflow_task_failed: "Failed decoding arguments"` every ~10 s with the
    worker re-polling the poisoned task forever; and a child reading an absent optional key from
    its payload hung the child, the parent, and the session's expectation with it. Neither parent
    carries an `execution_timeout` of its own, so nothing ends either one.

    Scoped to the job path rather than to every registered workflow, deliberately: these are the
    runs a person is waiting on. The periodic workflows were decided one at a time instead
    (`D-2026-08-27-a-periodic-job-decides-for-itself-whether-a-bug-should-park-it`), and
    `test_every_background_workflow_holds_the_stance_argued_for_it` below is where those decisions
    live — including the six that deliberately keep parking.

    The check is over the *registry* rather than a list of names, so a bundle added later is
    covered without editing this file — which is the property the seam claims and the reason the
    hole existed at all: nothing checked it.
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
# `D-2026-08-27-a-periodic-job-decides-for-itself-whether-a-bug-should-park-it`. That ADR carries
# the reason for every name here and each workflow carries its own beside its decorator; this is
# the table a test can read.
#
# **The rule the split follows**, so a new workflow can be placed rather than guessed at: a plain
# exception in workflow code parks the run in the SDK's unbounded workflow-task-failure loop.
# Declare `failure_exception_types` where that park has **no ceiling, or a ceiling somebody is
# waiting through** — a workflow a tool, a CLI or a webhook starts (none of those sites passes an
# `execution_timeout`), or a fan-out child whose hour of `fan_out_child_timeout_seconds` is charged
# to a parent that a chemist is polling. Leave it parking where the only starter is a Temporal
# Schedule: that action carries `schedule_run_timeout_seconds`, nothing reads the result, and every
# such job is cursored or idempotent, so the fires it skips cost a delay and not a record.
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
        # Fan-out children of those. A parked child is dropped only when its execution timeout
        # expires, and that hour is spent by the parent the chemist is polling.
        "PublishNoteWorkflow",
        "ReportSectionWorkflow",
        # An uncapped second starter beside the Schedule: a merge webhook
        # (`request_note_reindex`) and the live lane's backfill, which awaits `handle.result()`.
        "NoteReindexWorkflow",
        "ElnSyncWorkflow",
        # Schedule-only, and declared anyway: its output is a claim about a moment, so a resumed
        # park delivers a day-old verdict as current.
        "EvalDriftWorkflow",
        # Schedule-only drains, declared by their own merged argument before this table existed
        # (`corpus_sync.py`, `label_sync.py`). D-2026-08-27 records that the rule above would not
        # have required either, and that reversing a merged declaration on a tie is not worth the
        # churn — the entry is here so removing one is a decision someone takes, not a tidy-up.
        "ReactionCorpusWorkflow",
        "ReactionLabelWorkflow",
    }
)

# Deliberate non-changes. Each is reached only from a Temporal Schedule, so the park is bounded by
# `schedule_run_timeout_seconds`; nothing reads the run; and the work is idempotent or cursored, so
# a skipped fire costs a delay. Declaring these would buy a failure state no surface reports —
# `ScheduleHealth` carries no run outcome — and cost the run its chance to finish once a same-day
# redeploy fixes the bug.
_MAY_PARK = frozenset(
    {
        "ArtifactEvictionWorkflow",
        "DigestWorkflow",
        "DocumentShareSyncWorkflow",
        "ObservationSynthesisWorkflow",
        "PublishResultsWorkflow",
        "RetentionWorkflow",
    }
)


def _real_background_workflows() -> dict[str, type]:
    """The `@workflow.defn` classes on core's queue, by name.

    Filtered to classes Temporal actually has a definition for, because two tests above register
    bare `type()` probes on `background` to exercise the collision guard. Keying on the presence of
    `__temporal_workflow_definition` is the same fact the stance check reads, so a probe cannot
    make this table look incomplete and a real workflow cannot hide behind the filter.
    """
    return {
        cls.__name__: cls
        for cls in registered_workflows("background")
        if getattr(cls, "__temporal_workflow_definition", None) is not None
    }


def _declares_failure(cls: type) -> bool:
    """Whether `cls` turns a plain `Exception` in workflow code into a workflow *failure*.

    Read off the definition Temporal itself consults (`workflow_is_failure_exception`), and
    checking that `Exception` is actually covered rather than that the tuple is merely non-empty —
    `failure_exception_types=[ValueError]` is a different decision from this one.
    """
    definition = getattr(cls, "__temporal_workflow_definition", None)
    declared = getattr(definition, "failure_exception_types", ()) or ()
    return any(issubclass(Exception, declared_type) for declared_type in declared)


def test_every_background_workflow_holds_the_stance_argued_for_it() -> None:
    """Each periodic workflow either fails or parks *because someone argued it should*.

    The backlog row this closes asked for a decision per workflow and said in as many words that
    widening the job-path assertion across all of them was the wrong answer. So this asserts both
    directions: a workflow in `_MUST_FAIL` that stops declaring goes red, **and a workflow in
    `_MAY_PARK` that starts declaring goes red too**. The second half is what makes this a record
    of decisions rather than a snapshot of the tree — flipping a stance means revisiting the
    argument in D-2026-08-27 and moving the name, which is a diff a reviewer can see.

    What the two stances actually *do* is measured rather than assumed, by
    `test_the_two_stances_behave_as_the_table_assumes` below — because this table would be a table
    of decorator spellings otherwise.
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

    The whole decision table above rests on a claim about the SDK — that an exception raised in
    workflow *code* parks the run rather than failing it, unless the definition says otherwise —
    and a table resting on documentation is a table of spellings. So the claim is run.

    Measured here on the time-skipping server: the declared workflow reached `FAILED` with an
    `ApplicationError` on its first attempt, and the undeclared one was still `RUNNING` five
    seconds later, the worker re-failing and re-polling the same poisoned task. Nothing ends the
    second one — neither start carries an `execution_timeout`, which is exactly the shape of the
    tool- and webhook-started workflows in `_MUST_FAIL`.
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
