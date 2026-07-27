"""Durable capabilities declare their own queue, and the workers serve what is declared.

The failure this guards is silent and was hit for real while building the xTB job: a
workflow that is written, tested and imported but missing from a worker's hardcoded list
never runs, and nothing fails until someone submits one and it waits in the queue
forever.
"""

import pytest

from workflows.registry import (
    describe,
    durable_workflow,
    registered_activities,
    registered_workflows,
)


def test_every_declared_capability_reaches_its_worker() -> None:
    """The workers serve exactly what the registry holds for their queue.

    Importing the worker modules is what registers their capabilities, so this also
    proves the imports are still there — the one thing adding a workflow to a *new*
    module still requires.
    """
    from workers.background_worker import BACKGROUND_ACTIVITIES, BACKGROUND_WORKFLOWS
    from workers.hpc_worker import HPC_ACTIVITIES, HPC_WORKFLOWS

    assert HPC_WORKFLOWS == registered_workflows("hpc")
    assert HPC_ACTIVITIES == registered_activities("hpc")
    assert BACKGROUND_WORKFLOWS == registered_workflows("background")
    assert BACKGROUND_ACTIVITIES == registered_activities("background")


def test_the_queues_do_not_overlap() -> None:
    """A capability belongs to one queue. Two would mean two workers racing for it."""
    from workers import background_worker, hpc_worker  # noqa: F401 — registration

    for kind in (registered_workflows, registered_activities):
        hpc = {item.__name__ for item in kind("hpc")}
        background = {item.__name__ for item in kind("background")}
        assert hpc.isdisjoint(background)


def test_the_xtb_job_is_registered_on_the_heavy_queue() -> None:
    """The capability this registry was built while adding, pinned where it belongs.

    xTB jobs are the expensive ones — minute-scale on drug-sized molecules — so they
    belong with the few heavy workers (`hpc`), not with the many light ones (D-006).
    """
    from workflows.xtb_activities import run_xtb_calculation
    from workflows.xtb_job import XtbJobWorkflow

    assert XtbJobWorkflow in registered_workflows("hpc")
    assert run_xtb_calculation in registered_activities("hpc")


def _probe(name: str, module: str) -> type:
    """A stand-in for a decorated workflow class; the registry reads only these two."""
    return type(name, (), {"__module__": module, "__doc__": "probe"})


def test_a_name_claimed_by_two_modules_is_rejected() -> None:
    """Two definitions sharing a Temporal name means the worker silently drops one."""
    first = _probe("RegistryCollisionProbe", "workflows.probe_one")
    durable_workflow("background")(first)
    with pytest.raises(ValueError, match="claimed by both"):
        durable_workflow("background")(_probe("RegistryCollisionProbe", "workflows.probe_two"))
    assert first in registered_workflows("background")


def test_re_registering_the_same_definition_is_allowed() -> None:
    """Temporal's workflow sandbox re-imports workflow modules, re-running the decorator.

    So the guard compares the defining *module* rather than object identity — otherwise
    every workflow task would raise on a duplicate that is not one, and the worker would
    die on its first piece of work.
    """
    module = "workflows.reimport_probe"
    durable_workflow("background")(_probe("RegistryReimportProbe", module))
    durable_workflow("background")(_probe("RegistryReimportProbe", module))  # must not raise
    names = [item.__name__ for item in registered_workflows("background")]
    assert names.count("RegistryReimportProbe") == 1


def test_describe_names_what_a_worker_serves() -> None:
    """The startup log line is derived, not restated — so it cannot go stale."""
    line = describe("hpc")
    assert "XtbJobWorkflow" in line
    assert "run_xtb_calculation" in line
