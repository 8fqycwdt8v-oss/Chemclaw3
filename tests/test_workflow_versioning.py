"""A command added to a live workflow path is a replay break unless it is patched.

**This file exists because the tree's first two `workflow.patched` calls were added after the
defect, not before it.** `D-2026-09-14-a-declared-kind-with-no-producer-is-not-a-channel` gave four
workflows an outbound copy, and two of those calls landed *inside* a path that live runs had already
executed. Temporal replays a workflow by matching the command sequence its code emits against the
one its history records, so an `await` that was not there before is not a new feature to an open
run — it is a mismatch:

- `AwaitAnswerWorkflow._push` runs before `_wait_until`, so an open wait has `TimerStarted` where
  the new code emits `ActivityTaskScheduled`. Measured: `[TMPRL1100] Nondeterminism error: Activity
  machine does not handle this event`. That workflow is `failure_exception_types=[Exception]`, and
  a `NondeterminismError` is an `ApplicationError`, so it **fails the wait** rather than parking —
  leaving a `pending_requests` row `waiting` with no run that will settle it, for up to
  `awaiting_max_days`.
- `DigestWorkflow` emitted a *different activity type* at a position its history already held.
  Measured: `Activity type of scheduled event 'deliver_digest_activity' does not match activity
  type of activity command 'deliver_message_activity'`. It declares no `failure_exception_types`,
  so it parks its workflow task forever — and the digest is a Schedule under
  `ScheduleOverlapPolicy.SKIP`, so one wedged run silently skips every subsequent night.

Neither is checkable in general: no test can know which edit to a workflow body adds a command to a
path some run has already passed. What *is* checkable is the two ways a correct patch is undone
afterwards, and both are silent — reusing an id, and deleting the code the off branch still needs.
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

    `workflow.patched` records one marker per id. A second site reading the same id sees the first
    site's marker and takes the *new* branch on a history that never ran it — which is the very
    mismatch the patch was added to avoid, now with the guard reporting success.
    """
    ids = [patch_id for _, patch_id in _patch_ids()]
    assert len(ids) == len(set(ids)), f"a patch id is used twice: {sorted(ids)}"


def test_a_patched_off_branch_still_has_the_code_its_histories_recorded() -> None:
    """The deprecated digest activity is what an open run replays; deleting it is the original bug.

    Asserted by name rather than by behaviour because behaviour is exactly what it must not have:
    it is scheduled only on the branch no new run takes. The guard is that the symbol survives a
    tidying pass — "nothing calls this" is true of it and is not a reason to remove it.
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
    }
    assert {patch_id for _, patch_id in _patch_ids()} == declared
