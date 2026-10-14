"""A private Temporal dev server for the multi-replica lane: `python -m ... <port>`.

A process of its own, not a fixture, because the server must outlive every `asyncio.run` the lane
makes. It shuts down on SIGTERM or when its standard input closes, and the second is what keeps the
broker binary from being orphaned when the lane's runner is SIGKILLed: killing this process would
leave the binary behind, but the closing pipe lets it stop the binary first. Prints `ready` once it
accepts connections.
"""

import asyncio
import os
import signal
import sys

from temporalio.testing import WorkflowEnvironment


async def serve(port: int) -> None:
    """Run the dev server on `port` until SIGTERM or the end of standard input."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stop.set)

    async def parent_gone() -> None:
        await asyncio.to_thread(sys.stdin.buffer.read)  # returns at end of file
        stop.set()

    watcher = asyncio.create_task(parent_gone())
    environment = await WorkflowEnvironment.start_local(port=port)
    print("ready", flush=True)
    await stop.wait()
    await environment.shutdown()
    watcher.cancel()
    # The thread reading standard input cannot be interrupted; the broker is already stopped.
    os._exit(0)


if __name__ == "__main__":
    asyncio.run(serve(int(sys.argv[1])))
