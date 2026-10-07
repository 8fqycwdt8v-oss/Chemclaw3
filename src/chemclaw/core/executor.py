"""The one thread pool every `asyncio.to_thread` in a process shares — sized, not defaulted.

`asyncio.to_thread` uses the loop's default executor, which CPython sizes `min(32, cpu + 4)`. That
pool is shared by bearer-token validation, retrieval and graph legs, embeddings and attachment
parses, so the process's own admission caps can fill it and leave token validation queued behind
a corpus parse.

So each process states how many threads its admission caps may hold at once and gets a pool that
wide plus `service_thread_pool_headroom`, reserved for the short calls that must never wait
(token validation, readiness probes, SSE reconnects). For the front door the reserved count is a
product, not a sum: each admitted turn may fan out to `agent_max_parallel_tool_calls` offloads.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from chemclaw.core.config import settings

logger = logging.getLogger(__name__)


def install_default_executor(*, component: str, reserved: int) -> ThreadPoolExecutor:
    """Give the running loop a default executor wider than this process's own concurrency caps.

    Call once, before anything offloads (the front door's lifespan, each worker's `serve_worker`).
    Every `asyncio.to_thread` and `run_in_executor(None, ...)` then lands in it; `asyncio.run` shuts
    it down on exit.

    Args:
        component: What this process is (`front-door`, `background-worker`), for the log line.
        reserved: How many threads this process's own admission caps can occupy at once —
            `front_door_reserved()` for the front door, `worker_max_concurrent_activities` for a
            worker. Stated by the caller because only the process knows which caps apply to it.

    Returns:
        The installed executor, so a caller can assert on its width.
    """
    max_workers = reserved + settings.service_thread_pool_headroom
    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="chemclaw")
    asyncio.get_running_loop().set_default_executor(executor)
    logger.info(
        "%s: to_thread pool sized %d (%d reserved for admitted work, %d headroom)",
        component,
        max_workers,
        reserved,
        settings.service_thread_pool_headroom,
    )
    return executor


def front_door_reserved() -> int:
    """How many threads the front door's own admission caps can occupy at the same time.

    `turns x parallel tool calls + parses`: an admitted turn may fan out to
    `agent_max_parallel_tool_calls` concurrent offloads, so summing the caps undersizes the pool.
    `attachment_max_concurrent_parses` already counts offloads and enters as it stands. A parallel
    cap of 0 means unbounded fan-out, which no finite pool covers, so it is charged as 1 (`max(1,
    ...)`). Read at call time so a test overriding a cap sees the width change.
    """
    return (
        settings.service_max_concurrent_turns * max(1, settings.agent_max_parallel_tool_calls)
        + settings.attachment_max_concurrent_parses
    )
