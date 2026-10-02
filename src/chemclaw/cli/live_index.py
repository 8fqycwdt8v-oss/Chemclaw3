"""`python -m chemclaw.cli.live_index` — bring the live lane's derived reaction indexes current.

The four-repo lane's bring-up ingests the seeded corpus (`cli/live_data --backfill-only`) and, until
this step, stopped there. Two derived indexes that a deployment keeps current on its own were never
brought current on the lane, and the structure-search tools said so on every run (#520):

* **Reaction labels.** Ingest writes only a reaction's *record* phase. The atom map, the named
  reaction and the per-species roles come from `ReactionLabelWorkflow`, which a deployment fires
  from the `reaction-labels` Schedule against `Chemclaw3-mcp`'s `rxnlabel` server. The lane applies
  no Schedule and started no labeller, so every row stayed stale and `substrate_precedent`
  answered "NOT ANSWERABLE YET: 4282 reaction(s) match … NONE of them have been labelled".
* **Fingerprint generations.** A lane's database outlives `down`/`up`, so rows written under an
  older fingerprint definition stay in it, and `similar_reactions` reported `index_partial` — "part
  of the reaction index is stored under a SUPERSEDED fingerprint definition and was not compared".
  A deployment carries rows across a definition bump with `make rekey-compounds APPLY=1` and then
  disposes of the shelved generation under the owning principal (`094`'s header names the
  statement); the lane did neither.

So this runs exactly those jobs, on the real broker and the real stores: one label drain (started,
or rejoined if one is already running), the operator's re-key, and — only when the re-key rebuilt
every shelved row — the disposal that lets `index_partial` read False again. **Bounded by one
deadline** (`--timeout`): the drain keeps running on the broker past it, the re-key is
interrupt-safe and idempotent (`memory.compound_rekey`), and a re-run converges on the same state,
so a bring-up never blocks on a large corpus.

**Why the drain is one run and not the Schedule.** The lane applies no Schedule at all — the ELN
sync is also a one-shot (`live_data.backfill`) — because a Schedule persists in Temporal past the
lane's `down`. Rows ingested after the drain finishes are labelled by the next bring-up, which runs
this again.

**The disposal is an operator statement, and that is why it lives here.** The runtime role holds
no `DELETE` on either fingerprint table (`infra/sql/grants/app_privileges.sql`), deliberately; the
lane connects as the principal that owns the schema, which is the standing `094` names for this
statement. `tests/test_database_privileges.py` lists this module beside `cli/rekey_campaigns.py`
for the same reason.
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
from chemclaw.core.db import connection as db_connection
from chemclaw.core.logging import configure_logging
from chemclaw.core.temporal_client import connect as temporal_connect
from chemclaw.durable.label_sync import LabelSyncOutcome, ReactionLabelWorkflow
from chemclaw.science.fingerprints.molfp.fingerprint import molecule_definition
from chemclaw.science.fingerprints.rekey import FingerprintRekeyCounts
from chemclaw.science.fingerprints.rxnfp.fingerprint import reaction_definition

logger = logging.getLogger(__name__)

#: A fixed id, so a second bring-up rejoins a drain that is still running instead of racing it —
#: `live_data.backfill`'s argument: two drains over one stale set contend on the same rows and
#: produce nothing the first would not. A finished run's id is free again, so the next bring-up
#: starts a fresh one over whatever has gone stale since.
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

    **A drain still running is a state, not an error** — the same reading `live_data.backfill`
    gives the ELN drain. It keeps going on the broker, and the label coverage the precedent tools
    report is the honest number for how far it got.
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


async def dispose_superseded(table: str, definition: str) -> int:
    """Delete `table`'s rows stored under any definition other than `definition`; return how many.

    The statement `infra/sql/094_fingerprint_definition_identity.sql` names for "disposing of a
    shelved generation after a completed rebuild". Interpolated, so `table` is checked to be a plain
    identifier — every caller passes a constant, and this is the trust boundary
    `PostgresFingerprintStore` draws for the same reason.
    """
    if not table.isidentifier():
        raise ValueError(f"table must be a plain SQL identifier, got {table!r}")
    async with db_connection(settings.postgres_dsn, operation="live-index-dispose") as conn:
        cursor = await conn.execute(
            f"DELETE FROM {table} WHERE definition <> %(definition)s", {"definition": definition}
        )
        return cursor.rowcount


async def settle_index(
    kind: str, table: str, definition: str, counts: FingerprintRekeyCounts
) -> str:
    """Dispose of `table`'s shelved generation when the re-key rebuilt all of it, and say which.

    **Complete means `unreadable == 0`.** Every other shelved row the re-key saw was rebuilt
    (`rekeyed`) or already had a current row (`already_current`), so deleting the superseded
    generation loses nothing the current one does not hold. An unreadable row is the one case where
    it would: its label no longer parses, so it has no current twin, and dropping it would turn a
    search that honestly says PARTIAL into one that silently never had the row. Then the shelf
    stays and `index_partial` keeps saying so.
    """
    if counts.unreadable:
        return (
            f"{kind}: {counts.unreadable} shelved row(s) could not be rebuilt, so the superseded "
            "generation is kept and searches still say PARTIAL — re-sync those entries from source"
        )
    removed = await dispose_superseded(table, definition)
    return (
        f"{kind}: {counts.rekeyed} re-fingerprinted, {counts.current} already current, "
        f"{removed} superseded row(s) disposed of"
    )


async def rebuild_fingerprints(timeout_seconds: float) -> list[str]:
    """The operator's re-key, applied, then each index's disposal if its rebuild completed."""
    report = await asyncio.wait_for(_rekey(apply=True), timeout=max(timeout_seconds, 0.0))
    notes = report.notes
    return [
        f"compound notes ({report.version}): {notes['successors']} successor(s) written, "
        f"{notes['retired']} retired, {notes['blocked_successor']} blocked",
        await settle_index(
            "reaction fingerprints",
            "reaction_fingerprints",
            reaction_definition(),
            report.reactions,
        ),
        await settle_index(
            "molecule fingerprints",
            "molecule_fingerprints",
            molecule_definition(),
            report.molecules,
        ),
    ]


async def bring_current(timeout_seconds: float, client: Client | None = None) -> IndexRun:
    """Start the label drain, rebuild the fingerprints meanwhile, then wait out the deadline.

    The drain is started first because it is the long half and runs on the broker regardless; the
    re-key runs in this process while the drain works, and whatever is left of the one deadline is
    spent waiting on the drain. A step that raises is reported and marks the run failed without
    stopping the other — the two indexes are independent, and half a fix is still a fix.
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
