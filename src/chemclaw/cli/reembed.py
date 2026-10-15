"""`make reembed`: bring every stored vector to the configured embedding model and width.

Run after changing `CHEMCLAW_EMBEDDING_MODEL` or `CHEMCLAW_EMBEDDING_DIM` on the pods, once the
migration has built the column for the new width (`make db-migrate`, or the chart's pre-upgrade
hook). Starts `EmbeddingReembedWorkflow` on the background queue, or rejoins the one running, and
waits for its verdict; the run is idempotent, so a second start after a failure only finishes it.

    python -m chemclaw.cli.reembed            # start (or rejoin) and wait
    python -m chemclaw.cli.reembed --status   # which configurations the corpus holds, no Temporal

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
from chemclaw.durable.reembed import EmbeddingReembedWorkflow, ReembedOutcome

#: A fixed id, so a second start rejoins a running re-embed instead of racing it.
WORKFLOW_ID = "embedding-reembed"

_EXIT_INCOMPLETE = 1
_EXIT_REFUSED = 2


async def status() -> str:
    """One line per vector table: the live column, and the other configurations stored in it."""
    lines = [f"configured: {embedding_config_key()}"]
    async with db.connection(settings.postgres_dsn) as conn:
        for state in await inspect_tables(conn, settings.embedding_dim, live_embedding_keys()):
            width = "missing" if state.column_width is None else f"vector({state.column_width})"
            lines.append(
                f"{state.table}: {state.column} {width}; "
                f"other configurations: {state.describe_foreign()}"
            )
    return "\n".join(lines)


async def run() -> tuple[str, int]:
    """Start (or rejoin) the workflow, wait, and return its summary and the exit status."""
    client = await connect()
    try:
        handle = await client.start_workflow(
            EmbeddingReembedWorkflow.run,
            id=WORKFLOW_ID,
            task_queue=settings.background_task_queue,
        )
        print(f"started {WORKFLOW_ID}")
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(WORKFLOW_ID)
        print(f"rejoining {WORKFLOW_ID}, which is already running")
    try:
        outcome: ReembedOutcome = await handle.result()
    except WorkflowFailureError as exc:
        return f"{WORKFLOW_ID} refused: {exc.cause}", _EXIT_REFUSED
    summary = (
        f"documents embedded {outcome.documents_embedded} (failed {outcome.documents_failed}); "
        f"notes indexed {outcome.notes_indexed}; old vectors cleared {outcome.vectors_cleared}; "
        f"still under another configuration: {outcome.remaining.documents} chunk(s), "
        f"{outcome.remaining.notes} note(s)"
    )
    return summary, 0 if outcome.complete else _EXIT_INCOMPLETE


def main() -> int:
    """Parse the flags, print the status or the run's verdict, and return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status", action="store_true", help="print the stored configurations and exit"
    )
    args = parser.parse_args()
    configure_logging()
    if args.status:
        print(asyncio.run(status()))
        return 0
    summary, code = asyncio.run(run())
    print(summary)
    return code


if __name__ == "__main__":
    sys.exit(main())
