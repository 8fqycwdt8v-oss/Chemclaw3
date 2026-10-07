"""`python -m chemclaw.cli.live_index` — bring the live lane's derived reaction indexes current.

Ingest writes only a reaction's record phase, so this runs the jobs a deployment runs on its own:

* **Reaction labels**: one `ReactionLabelWorkflow` drain (started, or rejoined if running) against
  `Chemclaw3-mcp`'s `rxnlabel`, instead of the `reaction-labels` Schedule — a Schedule would
  persist in Temporal past the lane's `down`.
* **Fingerprint generations**: the operator's re-key, then — only when every shelved row was
  rebuilt — `rekey_compounds.settle_indexes`, the same disposal `make rekey-compounds APPLY=1
  DISPOSE=1` runs. It connects as the schema owner, since the runtime role holds no `DELETE` on
  the fingerprint tables.

Bounded by one `--timeout`: the drain keeps running on the broker past it, the re-key is
interrupt-safe and idempotent, and a re-run converges.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from temporalio.client import Client, WorkflowHandle
from temporalio.exceptions import WorkflowAlreadyStartedError

from chemclaw.cli import rekey_compounds
from chemclaw.core.config import settings
from chemclaw.core.logging import configure_logging
from chemclaw.core.temporal_client import connect as temporal_connect
from chemclaw.durable.label_sync import LabelSyncOutcome, ReactionLabelWorkflow

logger = logging.getLogger(__name__)

# : A fixed id, so a second bring-up rejoins a running drain instead of racing it on the same rows.
# : A finished run's id is free again.
LABEL_DRAIN_ID = "reaction-labels-lane-drain"

# Module-level indirection, so a test swaps the re-key for one over its own stores — the shape
# `durable/label_sync.py` uses for its index and server.
_rekey = rekey_compounds.rekey


@dataclass
class IndexRun:
    """What one pass did, as the lines a bring-up log shows and the exit code it returns."""

    lines: list[str] = field(default_factory=list)
    failed: bool = False


DrainHandle = WorkflowHandle[ReactionLabelWorkflow, LabelSyncOutcome]


async def start_label_drain(client: Client) -> DrainHandle:
    """Start one `ReactionLabelWorkflow` on the background queue, or rejoin the one running."""
    try:
        handle = await client.start_workflow(
            ReactionLabelWorkflow.run,
            id=LABEL_DRAIN_ID,
            task_queue=settings.background_task_queue,
        )
        logger.info("label drain %s started on %s", LABEL_DRAIN_ID, settings.background_task_queue)
        return handle
    except WorkflowAlreadyStartedError:
        logger.info("label drain %s already running — waiting on it", LABEL_DRAIN_ID)
        return client.get_workflow_handle_for(ReactionLabelWorkflow.run, LABEL_DRAIN_ID)


async def finish_label_drain(handle: DrainHandle, timeout_seconds: float) -> str:
    """Wait up to `timeout_seconds` for the drain and say what it did.

    A drain still running is a state, not an error: it continues on the broker.
    """
    try:
        outcome = await asyncio.wait_for(handle.result(), timeout=max(timeout_seconds, 0.0))
    except TimeoutError:
        return (
            f"{LABEL_DRAIN_ID}: still draining at the deadline — it keeps running on the broker, "
            "and the precedent tools' `coverage` says how far it has got"
        )
    # `unlabelled` is reported beside `labelled` because a pass that stamps rows and derives nothing
    # is a broken labeller reporting progress (`LabelSyncOutcome`), and one total cannot say so.
    return (
        f"{LABEL_DRAIN_ID}: labelled {outcome.labelled} reaction(s), "
        f"{outcome.unlabelled} stamped with nothing derived"
    )


async def rebuild_fingerprints(timeout_seconds: float) -> list[str]:
    """The operator's re-key, applied, then each index's disposal if its rebuild completed."""
    report = await asyncio.wait_for(_rekey(apply=True), timeout=max(timeout_seconds, 0.0))
    notes = report.notes
    return [
        f"compound notes ({report.version}): {notes['successors']} successor(s) written, "
        f"{notes['retired']} retired, {notes['blocked_successor']} blocked",
        *await rekey_compounds.settle_indexes(report),
    ]


async def bring_current(timeout_seconds: float, client: Client | None = None) -> IndexRun:
    """Start the label drain, rebuild the fingerprints meanwhile, then wait out the deadline.

    The re-key runs in this process while the drain runs on the broker. A step that raises marks the
    run failed without stopping the other; the two indexes are independent.
    """
    deadline = time.monotonic() + timeout_seconds
    run = IndexRun()
    handle = None
    try:
        handle = await start_label_drain(client if client is not None else await temporal_connect())
    except Exception as exc:  # reported as the step's outcome, never swallowed
        run.failed = True
        run.lines.append(f"Labels: FAILED to start {LABEL_DRAIN_ID} — {type(exc).__name__}: {exc}")
    try:
        for line in await rebuild_fingerprints(deadline - time.monotonic()):
            run.lines.append(f"Fingerprints: {line}")
    except TimeoutError:
        run.lines.append(
            "Fingerprints: the re-key was still running at the deadline — it is interrupt-safe "
            "and idempotent, so the next run carries on where this one stopped"
        )
    except Exception as exc:  # reported as the step's outcome, never swallowed
        run.failed = True
        run.lines.append(f"Fingerprints: FAILED — {type(exc).__name__}: {exc}")
    if handle is not None:
        try:
            drained = await finish_label_drain(handle, deadline - time.monotonic())
            run.lines.append(f"Labels: {drained}")
        except Exception as exc:  # a failed workflow is this step's outcome
            run.failed = True
            run.lines.append(f"Labels: FAILED — {type(exc).__name__}: {exc}")
    return run


def main(argv: Sequence[str] | None = None) -> int:
    """Bring the lane's reaction indexes current; exit non-zero when a step failed."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="seconds this run may take in all; the label drain keeps running past it",
    )
    args = parser.parse_args(argv)
    configure_logging()
    run = asyncio.run(bring_current(args.timeout))
    print("\n".join(run.lines))
    return 1 if run.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
