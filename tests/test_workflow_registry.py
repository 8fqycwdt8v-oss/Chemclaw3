"""Durable capabilities declare their own queue, and the workers serve what is declared.

The failure this guards is silent and was hit for real while building the xTB job: a
workflow that is written, tested and imported but missing from a worker's hardcoded list
never runs, and nothing fails until someone submits one and it waits in the queue
forever.
"""

import json
import subprocess
import sys
import textwrap

import pytest

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
    import chemclaw.connectors.calc.worker
    import chemclaw.connectors.qm.worker  # noqa: F401 — registration
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
    from chemclaw.connectors.calc.activities import run_xtb_calculation
    from chemclaw.connectors.calc.workflows import CalcJobWorkflow
    from chemclaw.connectors.qm.activities import submit_to_hpc
    from chemclaw.connectors.qm.workflows import QMJobWorkflow
    from chemclaw.connectors.queues import bundle_queue

    for workflow_cls, activity, bundle in (
        (CalcJobWorkflow, run_xtb_calculation, "calc"),
        (QMJobWorkflow, submit_to_hpc, "qm"),
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
    import chemclaw.connectors.qm.worker  # noqa: F401 — registration
    from chemclaw.connectors.queues import bundle_queue

    line = describe(bundle_queue("qm"))
    assert "workflows=[QMJobWorkflow]" in line
    assert "parse_qm_output" in line
