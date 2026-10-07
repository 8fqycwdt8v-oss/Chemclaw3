"""A command added to a live workflow path is a replay break unless it is patched.

Temporal replays by matching the command sequence the code emits against the history, so a new
`await` on a path open runs already passed is a nondeterminism error: it fails a workflow that
declares `failure_exception_types` and parks one that does not. Which edit adds such a command is
not checkable in general; what is checkable is the two silent ways a correct patch is undone
afterwards — reusing an id, and deleting the code the off branch still needs.
"""

import ast
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"
_PATCH_CALL = re.compile(r"workflow\.patched\(\s*\"([^\"]+)\"\s*\)")


def _patch_ids() -> list[tuple[str, str]]:
    """Every `workflow.patched("...")` in the tree, as `(module, id)` in file order."""
    found: list[tuple[str, str]] = []
    for path in sorted(_SRC.rglob("*.py")):
        for patch_id in _PATCH_CALL.findall(path.read_text(encoding="utf-8")):
            found.append((str(path.relative_to(_SRC)), patch_id))
    return found


def test_no_patch_id_is_ever_reused() -> None:
    """Two sites sharing an id is a replay break that no error names.

    `workflow.patched` records one marker per id, so a second site would take the new branch on a
    history that never ran it.
    """
    ids = [patch_id for _, patch_id in _patch_ids()]
    assert len(ids) == len(set(ids)), f"a patch id is used twice: {sorted(ids)}"


def test_a_patched_off_branch_still_has_the_code_its_histories_recorded() -> None:
    """The deprecated digest activity is what an open run replays; deleting it is the original bug.

    Asserted by name, because it runs only on the branch no new run takes; "nothing calls this" is
    not a reason to remove it.
    """
    from chemclaw.durable import digest

    assert hasattr(digest, "deliver_digest_activity"), (
        "the off branch of `digest-outbound-delivery-seam` schedules `deliver_digest_activity`; "
        "removing it makes every in-flight digest unreplayable rather than merely unpatched"
    )
    assert hasattr(digest, "DeliveryInput"), "its argument model goes with it"


def test_the_off_branch_is_reachable_from_the_workflow_that_needs_it() -> None:
    """The shim is referenced by `DigestWorkflow.run`, not merely defined beside it.

    A shim nothing schedules is a shim that was kept for the wrong reason, and would pass the test
    above while the workflow had already stopped emitting it.
    """
    from chemclaw.durable import digest

    tree = ast.parse(Path(digest.__file__).read_text(encoding="utf-8"))
    workflow_class = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "DigestWorkflow"
    )
    names = {node.id for node in ast.walk(workflow_class) if isinstance(node, ast.Name)}
    assert "deliver_digest_activity" in names, (
        "DigestWorkflow no longer schedules the deprecated activity, so the patch's off branch "
        "emits nothing where an open run's history holds a scheduled activity"
    )


def test_the_off_branch_activity_is_registered_on_the_queue_that_replays_it() -> None:
    """A symbol the worker does not serve is as unreplayable as a symbol that is gone.

    Without `@durable_activity("background")` the off-branch activity would exist but be
    unregistered, and a replaying run would fail with `Activity function ... is not registered on
    this worker`. So this asserts registration on the queue `DigestWorkflow` is registered on.
    """
    from chemclaw.durable import digest
    from chemclaw.durable.registry import (
        registered_activities,
        registered_workflows,
        temporal_name,
    )

    queue = "background"
    assert digest.DigestWorkflow in registered_workflows(queue), (
        "DigestWorkflow is not on the background queue any more; this test is reading the wrong one"
    )
    served = {temporal_name(one) for one in registered_activities(queue)}
    assert temporal_name(digest.deliver_digest_activity) in served, (
        "`deliver_digest_activity` is defined and referenced but not registered on the queue that "
        "replays it, so the off branch of `digest-outbound-delivery-seam` schedules an activity "
        "type the worker cannot resolve — the same wedge deleting it would cause, with both "
        "existing guards green"
    )


def test_every_durable_activity_is_registered_on_a_queue() -> None:
    """`@activity.defn` alone defines an activity nobody serves, and nothing else notices.

    `@activity.defn` says what the function is and `@durable_activity` says who serves it; one
    without the other is unreachable at run time on a queue whose tests all pass. Asserted over the
    whole package, because any decorator can be dropped in a tidying pass.
    """
    durable = Path(__file__).resolve().parents[1] / "src" / "chemclaw" / "durable"
    unserved: list[str] = []
    defined = 0
    for module in sorted(durable.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            decorators = [ast.unparse(one) for one in node.decorator_list]
            # `@activity.defn(name="…")` also declares an activity (the documented way to override
            # the name), so both spellings are matched.
            if not any(
                one == "activity.defn" or one.startswith("activity.defn(") for one in decorators
            ):
                continue
            defined += 1
            if not any(one.startswith("durable_activity(") for one in decorators):
                unserved.append(f"{module.relative_to(durable)}::{node.name}")
    assert defined, "no durable activities found; this test is reading the wrong tree"
    assert not unserved, (
        f"{unserved} are declared with `@activity.defn` and registered on no queue, so any "
        "workflow scheduling them fails at run time with `NotFoundError: Activity function ... is "
        "not registered on this worker` — while every queue's own tests pass"
    )


def test_every_patch_is_declared_in_this_file_so_its_removal_date_is_readable() -> None:
    """A patch is temporary by design, and nothing else in the tree records when it may go.

    Pinned as a set: adding one without a row here is red, which is the prompt to write down what
    an open run of that workflow looks like and when the branch stops being reachable.
    """
    declared = {
        # `AwaitAnswerWorkflow._push`. Removable once no wait opened before the release that added
        # it can still be running — bounded by `awaiting_max_days`, which ships at 90.
        "awaiting-outbound-delivery",
        # `DigestWorkflow.run`. Removable the day after it ships: the digest is a nightly Schedule
        # and a run completes in minutes, so no history older than one night can be replayed.
        "digest-outbound-delivery-seam",
        # `TemplateWorkflow.run`. Gates the resume read at the start and scheduling steps in waves,
        # which landed together. Removable once no run open at that release can still be executing,
        # bounded by `template_run_timeout_seconds`.
        "template-waves-and-resume",
        # `CheckInWorkflow.run`. Moves the stale-notice supersede from once per page to once per
        # batch. Removable the day after it ships, for the digest's reason: a nightly Schedule
        # whose run is bounded by its own interval, so no older history is still replayed.
        "check-in-supersede-per-batch",
        # `HypothesisTournamentWorkflow._settle`. Stops dispatching a check that names no target.
        # Started with no execution timeout, so check the namespace for open runs before removing.
        "tournament-empty-calls-refused-before-budget",
    }
    assert {patch_id for _, patch_id in _patch_ids()} == declared
