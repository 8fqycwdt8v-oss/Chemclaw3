"""A private Temporal dev server for the multi-replica lane: `python -m ... <port>`.

A process of its own, not a fixture, because the server must outlive every `asyncio.run` the lane
makes and be stopped without leaving the broker binary behind: SIGTERM shuts it down, while a
SIGKILL of this process would orphan it. Prints `ready` once it accepts connections.
"""

import asyncio
import signal
import sys

from temporalio.testing import WorkflowEnvironment


async def serve(port: int) -> None:
    """Run the dev server on `port` until SIGTERM."""
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    environment = await WorkflowEnvironment.start_local(port=port)
    print("ready", flush=True)
    await stop.wait()
    await environment.shutdown()


if __name__ == "__main__":
    asyncio.run(serve(int(sys.argv[1])))
