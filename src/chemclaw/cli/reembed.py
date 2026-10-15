"""`make reembed`: bring every stored vector to the configured embedding model and width.

Run after changing `CHEMCLAW_EMBEDDING_MODEL` or `CHEMCLAW_EMBEDDING_DIM` on the pods, once the
migration has built the column for the new width (`make db-migrate`, or the chart's pre-upgrade
hook). Starts `EmbeddingReembedWorkflow` on the background queue, or rejoins the one running, and
waits for its verdict; the run is idempotent, so a second start after a failure only finishes it.

    python -m chemclaw.cli.reembed            # start (or rejoin) and wait
    python -m chemclaw.cli.reembed --status   # which configurations the corpus holds, no Temporal
    python -m chemclaw.cli.reembed --reclaim  # empty the old width's vectors: no way back

`--reclaim` is separate and never implied. The previous image reads the old column, so until it is
emptied a rollback heals itself; afterwards a rollback re-embeds the corpus. Run it when the
release is settled, at the same moment the old columns would be dropped.

Exit status: 0 when every row is under the configured key, 1 when rows remain, 2 on a refusal.
"""

import argparse
import asyncio
import sys

from temporalio.client import WorkflowFailureError
from temporalio.exceptions import WorkflowAlreadyStartedError

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.embeddings import embedding_config_key
from chemclaw.core.logging import configure_logging
from chemclaw.core.temporal_client import connect
from chemclaw.core.vector_schema import inspect_tables, live_embedding_keys
from chemclaw.durable.document_sync import share_sources
from chemclaw.durable.reembed import EmbeddingReembedWorkflow, ReembedOutcome, ReembedRequest

#: Fixed ids, so a second start rejoins a running run instead of racing it.
WORKFLOW_ID = "embedding-reembed"
RECLAIM_WORKFLOW_ID = "embedding-reclaim"

_EXIT_INCOMPLETE = 1
_EXIT_REFUSED = 2


async def status() -> str:
    """One line per vector table: the live column, and the other configurations stored in it."""
    lines = [f"configured: {embedding_config_key()}"]
    if settings.vector_store_provider != "pgvector":
        lines.append(
            f"vectors live in the {settings.vector_store_provider} store, not in Postgres; "
            "there is no pgvector column to inspect"
        )
        return "\n".join(lines)
    chunkings = {share.share_binding().chunking_key for share in share_sources().values()}
    async with db.connection(settings.postgres_dsn) as conn:
        for state in await inspect_tables(
            conn, settings.embedding_dim, live_embedding_keys(), chunkings
        ):
            width = "missing" if state.column_width is None else f"vector({state.column_width})"
            lines.append(
                f"{state.table}: {state.column} {width}; "
                f"other configurations: {state.describe_foreign()}"
            )
    return "\n".join(lines)


def summarise(outcome: ReembedOutcome) -> str:
    """The run's verdict as the operator reads it: what moved, what failed, what is left."""
    parts = [
        f"documents embedded {outcome.documents_embedded}",
        f"failed {outcome.documents_failed}",
        f"notes indexed {outcome.notes_indexed}",
        f"old vectors cleared {outcome.vectors_cleared}",
        f"still under another configuration: {outcome.remaining.documents} chunk(s), "
        f"{outcome.remaining.notes} note(s)",
    ]
    if outcome.documents_failed:
        parts.append(
            f"{outcome.documents_failed} chunk(s) would not embed and keep their old vector; "
            "see the worker log for the reason"
        )
    if outcome.stalled:
        parts.append("stopped early: several batches in a row failed entirely")
    if outcome.notes_skipped:
        parts.append("the note reindex was held by another worker and indexed nothing; run again")
    return "; ".join(parts)


def _root_cause(exc: BaseException) -> BaseException:
    """The innermost failure of a chain: Temporal wraps a refusal in an activity and a workflow."""
    while exc.__cause__ is not None:
        exc = exc.__cause__
    return exc


async def run(*, reclaim: bool = False) -> tuple[str, int]:
    """Start (or rejoin) the workflow, wait, and return its summary and the exit status."""
    client = await connect()
    workflow_id = RECLAIM_WORKFLOW_ID if reclaim else WORKFLOW_ID
    try:
        handle = await client.start_workflow(
            EmbeddingReembedWorkflow.run,
            ReembedRequest(reclaim=reclaim),
            id=workflow_id,
            task_queue=settings.background_task_queue,
        )
        print(f"started {workflow_id}")
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id, result_type=ReembedOutcome)
        print(f"rejoining {workflow_id}, which is already running")
    try:
        outcome: ReembedOutcome = await handle.result()
    except WorkflowFailureError as exc:
        return f"{workflow_id} refused: {_root_cause(exc)}", _EXIT_REFUSED
    return summarise(outcome), 0 if outcome.complete else _EXIT_INCOMPLETE


def main() -> int:
    """Parse the flags, print the status or the run's verdict, and return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status", action="store_true", help="print the stored configurations and exit"
    )
    parser.add_argument(
        "--reclaim",
        action="store_true",
        help="empty the superseded width's vectors; irreversible, so a rollback must re-embed",
    )
    args = parser.parse_args()
    configure_logging()
    if args.status:
        print(asyncio.run(status()))
        return 0
    summary, code = asyncio.run(run(reclaim=args.reclaim))
    print(summary)
    return code


if __name__ == "__main__":
    sys.exit(main())
