"""The one thread pool every `asyncio.to_thread` in a process shares — sized, not defaulted.

`asyncio.to_thread` is `run_in_executor(None, ...)`: the loop's *single* default
`ThreadPoolExecutor`, which CPython sizes `min(32, cpu_count + 4)` — **8 on a 4-CPU pod**. That is
the whole offload budget of a process, and this system spends it on four unrelated things at once:
bearer-token validation (`api/auth.py`, on every authenticated request), the retrieval and
knowledge-graph legs a turn fans out, embeddings, and attachment parses. The front door pins itself
to one uvicorn worker, and `service_max_concurrent_turns` defaults to exactly **8** — so the
shipped admission cap can fill the pool on its own, and every subsequent request queues its token
validation behind a corpus parse. Measured: a queued short call waited 0.2 ms at 1 concurrent
`load_notes`, 565.5 ms at 8, 813.4 ms at 16.

Two properties make that worse rather than self-correcting. `kg.graph._corpus_lock` is a *blocking*
lock, so N concurrent cold readers occupy N threads for the duration of *one* parse; and the
obvious operator response to queuing — raise `service_max_concurrent_turns` — oversubscribes the
same pool further. `agent/attachments.py` names the hazard in its own docstring and can only cap
itself (`attachment_max_concurrent_parses`); nothing sized the pool the caps share.

So each process states, once, how many threads its own admission caps may hold at the same time,
and gets a pool that wide **plus** `service_thread_pool_headroom` — the threads reserved for the
calls that are microseconds long and must never wait: token validation, a readiness probe, an SSE
reconnect. Sizing the shared pool is what closes the measured gap; a dedicated executor for token
validation, so it cannot queue behind chemistry even in principle, is a further step nobody needs
yet.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from chemclaw.core.config import settings

logger = logging.getLogger(__name__)


def install_default_executor(*, component: str, reserved: int) -> ThreadPoolExecutor:
    """Give the running loop a default executor wider than this process's own concurrency caps.

    Call once, from the process's "about to serve" moment — the front door's lifespan and every
    worker's `serve_worker` — before anything can offload. The returned executor is the loop's
    default from that point on, so every `asyncio.to_thread` and every bare
    `run_in_executor(None, ...)` in the tree lands in it with no plumbing. `asyncio.run` shuts it
    down on the way out, the same as the pool it replaces.

    Args:
        component: What this process is (`front-door`, `background-worker`), for the one log line
            an operator reads when they want to know how wide the pool actually is.
        reserved: How many threads this process's *own* admission caps can occupy simultaneously —
            for the front door,
            `service_max_concurrent_turns * agent_max_parallel_tool_calls +
            attachment_max_concurrent_parses`, because an admitted turn is not one unit of demand:
            it fans out to that many tool calls, each able to take a thread. This said
            `service_max_concurrent_turns + …` and charged a turn 1 where the cap allows 8, which
            is the arithmetic that had one short offload waiting 853 ms at a pool of 18 and 0.7 ms
            at 74. `worker_max_concurrent_activities` for a worker. Stated by the caller because a
            process is the only thing that knows which caps apply to it, and derived from settings
            that already exist rather than restated as a number someone has to keep in step.

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
