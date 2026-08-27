"""`make synthesize`: start a memory-synthesis job without a model in the loop (D-2026-08-25).

The four miners run on demand, never on a Schedule — and after the Schedule went, their only
trigger was `agent/durable_tools.synthesize_memory`, an LLM tool: a chemist who wanted "mine the
corpus after this import" had to phrase it in chat and hope the model picked the tool, and an
operator finishing a backfill had no way to start one at all. Knowledge generation deserves a
switch a person can flip directly; this is that switch, going through the same workflows, the
same PR-gate and the same daily-dedup id as the tool.

Usage: `python -m chemclaw.cli.synthesize <kind> [--fresh] [--actor <id>]` — kinds as the tool
lists them (`campaign`, `playbook`, `optimization`, `observation-promotion`), `--fresh` to force
a re-mine when today's run predates the corpus change you care about.
"""

import argparse
import asyncio
import sys
from typing import cast

from temporalio.client import WorkflowFailureError

from chemclaw.agent.durable_tools import _MEMORY_JOBS, MemoryJobKind, _memory_job_id
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.temporal_client import connect


async def _start(kind: MemoryJobKind, fresh: bool, actor: str) -> str:
    """Start (or rejoin) the synthesis and wait for it; return a printable outcome."""
    token = set_current_identity(actor, frozenset())
    try:
        client = await connect()
        workflow_id = _memory_job_id(kind, fresh=fresh)
        handle = client.get_workflow_handle(workflow_id)
        try:
            started = await client.start_workflow(
                _MEMORY_JOBS[kind],
                id=workflow_id,
                task_queue=settings.background_task_queue,
            )
            handle = started
            print(f"started {workflow_id}")
        except Exception:
            # Already running or already ran today: rejoin it, which is the daily-dedup contract
            # (`_memory_job_id`); `--fresh` is the way past it.
            print(f"rejoining {workflow_id} (already started today; use --fresh to re-mine)")
        try:
            references = cast(list[str], await handle.result())
        except WorkflowFailureError as exc:
            return f"{workflow_id} failed: {exc.cause}"
        if not references:
            return f"{workflow_id}: the corpus supports nothing new"
        opened = "\n  ".join(references)
        return f"{workflow_id} proposed {len(references)} note(s):\n  {opened}"
    finally:
        reset_current_identity(token)


def main() -> int:
    """Parse the kind and flags, run the job to completion, and report what it proposed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=sorted(_MEMORY_JOBS))
    parser.add_argument("--fresh", action="store_true", help="force a new run despite today's")
    parser.add_argument(
        "--actor",
        default=settings.service_actor_id,
        help="who is asking — stamped on every proposal the run opens",
    )
    args = parser.parse_args()
    print(asyncio.run(_start(cast(MemoryJobKind, args.kind), args.fresh, args.actor)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
