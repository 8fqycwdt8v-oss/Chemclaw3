"""One re-embed pass in its own process, for `tests/test_embedding_width.py`.

Runs `reembed_claimed` to exhaustion against the database its environment names. Each batch's
chunk texts are appended to a log file *after* the claim and *before* the write, and the embedding
call then waits `hold` seconds, so a test can see what a live process holds and kill it holding it.

    python -m tests.reembed_probe <log> <hold seconds> <batch size> <chunking key>
"""

import asyncio
import json
import sys
import time
from pathlib import Path

import chemclaw.ingest.documents.sync as sync
from chemclaw.core import embeddings
from chemclaw.ingest.documents.index import PostgresDocumentIndex


def _install_recorder(log: Path, hold: float) -> None:
    """Wrap the embedder the pass calls so it records its batch, then waits."""
    real = embeddings.embed_texts

    def recording(texts: list[str], *, cache: bool = True) -> list[list[float]]:
        with log.open("a") as handle:
            handle.write(json.dumps(texts) + "\n")
        time.sleep(hold)
        return real(texts, cache=cache)

    # By namespace, not attribute: `sync` imports the name rather than exporting it, and the
    # function under test looks it up there.
    vars(sync)["embed_texts"] = recording


async def _drain(chunking: str, batch: int) -> int:
    """Re-embed claimed batches until none remains; return how many chunks this process wrote."""
    written = 0
    while True:
        report = await sync.reembed_claimed(PostgresDocumentIndex(), {chunking}, batch)
        written += report.embedded
        if not report.has_more:
            return written


def main(argv: list[str]) -> int:
    """Install the recorder, drain, and print the count written."""
    log, hold, batch, chunking = Path(argv[0]), float(argv[1]), int(argv[2]), argv[3]
    _install_recorder(log, hold)
    print(asyncio.run(_drain(chunking, batch)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
