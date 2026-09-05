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

**"How many threads a cap can hold" is not the cap, and for the front door it was off by 8x.**
An admitted *turn* is not one offload: `agent_max_parallel_tool_calls` becomes `max_concurrency` on
the turn's config (`agent/state.py`), so one turn may run that many tool calls at once and several
of the tools a turn reaches offload — `agent/graph_tools.py` threads `build_graph`/`load_notes`,
`retrieval/retrievers.py` threads the embedding. A permit is a licence to fan out, so the ceiling
is the product. `front_door_reserved` below is that arithmetic, stated once where the pool it sizes
is built.
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


def front_door_reserved() -> int:
    """How many threads the front door's own admission caps can occupy at the same time.

    **A separate function because the number is not the sum of the caps, and reading it as one is
    the defect.** `api/app.py` passed `service_max_concurrent_turns + attachment_max_concurrent_
    parses` — 14 at the shipped defaults — while an admitted turn may fan out to
    `agent_max_parallel_tool_calls` concurrent tool calls, each of which may hold a thread. The
    true ceiling is `turns x parallel tool calls + parses`: **98**, so the pool was sized for a
    seventh of what the process's own caps permit, and the headroom that exists so a token
    validation never queues behind chemistry was the first thing a fan-out consumed.

    Measured on a 4-core sandbox, 96 offloads of 200 ms each (half GIL-holding parse, half file
    I/O — the shape of a corpus read) with one short call submitted behind them: at the shipped
    width the short call waited **762.7 ms** at worst; at this width, **123.2 ms**. The median is
    1.2 ms either way, which is why only the tail is quoted: the failure is one authentication
    stalling behind a fan-out, not a slower service.

    **It does not multiply the parses.** `attachment_max_concurrent_parses` is already a count of
    simultaneous *offloads* — its own semaphore bounds the threads, not the requests holding them —
    so it enters the sum as it stands. Only the turn cap is a licence to fan out.

    **`max(1, ...)` because 0 means *no* bound on the fan-out**, and no finite pool covers that: a
    literal multiplication would then charge a turn nothing and hand back a width smaller than the
    sum this function exists to replace. The honest floor at that setting is one thread per admitted
    turn, with the headroom left best-effort — which is what removing the bound asks for.

    Returns:
        The `reserved` argument `install_default_executor` should be given in the front door. Read
        at call time rather than at import, because `Settings` is constructed once per process and
        a test that overrides a cap must see the width change with it.
    """
    return (
        settings.service_max_concurrent_turns * max(1, settings.agent_max_parallel_tool_calls)
        + settings.attachment_max_concurrent_parses
    )
