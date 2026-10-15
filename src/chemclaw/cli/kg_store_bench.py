"""Measure the Postgres note store: write throughput, graph query latency, memory per corpus size.

Run against a scratch schema this command creates and drops, never the running system's tables:
`uv run python -m chemclaw.cli.kg_store_bench --notes 20000`. Prints one JSON document. The numbers
answer the exit criteria of the knowledge-store wave (notes per second with four writers, a
neighbourhood at 20,000 notes, resident memory that does not grow with the corpus); nothing asserts
them, because they depend on the machine and on what else the database is doing.
"""

import argparse
import asyncio
import contextlib
import json
import os
import random
import statistics
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import date
from functools import partial
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from chemclaw.core.config import settings
from chemclaw.core.db import connection, pooling
from chemclaw.core.migrate import migrate
from chemclaw.kg.note import Note
from chemclaw.kg.postgres_store import PostgresNoteStore
from chemclaw.kg.store import NoteUnit

_WORDS = (
    "suzuki solvent base ligand palladium yield impurity screen scale-up ester amide thf dmf "
    "temperature stirring workup crystallisation filtration purity assay hplc residual"
).split()
_AS_OF = date(2026, 10, 15)


def _body(rng: random.Random, links: list[str]) -> str:
    """About 1.1 kB of prose with `links` cited in it, the size the baseline measures."""
    prose = " ".join(rng.choice(_WORDS) for _ in range(150))
    return f"{prose} " + " ".join(f"[[{target}]]" for target in links)


def _note(index: int, total: int, rng: random.Random) -> Note:
    """Note `index` of `total`, citing two others (a ring-and-chords graph, a few retired)."""
    links = [f"bench-{rng.randrange(total):06d}" for _ in range(2)]
    retired = index % 50 == 0 and index + 1 < total
    return Note(
        id=f"bench-{index:06d}",
        type="reaction",
        created_by="agent",
        valid_to=date(2026, 1, 1) if retired else None,
        relations=[],
        body=_body(rng, links + ([f"superseded-by:bench-{index + 1:06d}"] if retired else [])),
    )


def _scratch_dsn(base: str, schema: str) -> str:
    """`base` with `schema` first on the connection's search path (`public` keeps `vector`)."""
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}options={quote(f'-c search_path={schema},public')}"


def _rss_mb() -> float:
    """This process's resident set in MB, from `/proc/self/status`."""
    with open("/proc/self/status", encoding="utf-8") as status:
        for line in status:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    raise OSError("VmRSS is not reported on this platform")


def _percentiles(seconds: list[float]) -> dict[str, float]:
    """Median, 95th percentile and worst case of `seconds`, in milliseconds."""
    ordered = sorted(seconds)
    return {
        "p50_ms": round(1000 * statistics.median(ordered), 2),
        "p95_ms": round(1000 * ordered[int(0.95 * (len(ordered) - 1))], 2),
        "max_ms": round(1000 * ordered[-1], 2),
    }


async def _timed(call: Callable[[str], Awaitable[Any]], subjects: list[str]) -> dict[str, float]:
    """Latency of `call(subject)` for each subject."""
    samples = []
    for subject in subjects:
        started = time.perf_counter()
        await call(subject)
        samples.append(time.perf_counter() - started)
    return _percentiles(samples)


async def _write(store: PostgresNoteStore, notes: list[Note], writers: int) -> float:
    """Notes per second putting `notes` through `writers` concurrent writers."""
    shares = [notes[i::writers] for i in range(writers)]

    async def one(share: list[Note]) -> None:
        for note in share:
            await store.put(NoteUnit(note))

    started = time.perf_counter()
    await asyncio.gather(*(one(share) for share in shares))
    return len(notes) / (time.perf_counter() - started)


async def _write_with_dependency(
    store: PostgresNoteStore, notes: list[Note], writers: int
) -> float:
    """As `_write`, but every unit also names one shared dependency that already exists."""
    shared = Note(id="bench-shared", type="compound", created_by="agent", body="Shared compound.")
    await store.put(NoteUnit(shared))

    async def one(share: list[Note]) -> None:
        for note in share:
            await store.put(NoteUnit(note, dependencies=(shared,)))

    started = time.perf_counter()
    await asyncio.gather(*(one(notes[i::writers]) for i in range(writers)))
    return len(notes) / (time.perf_counter() - started)


async def _queries(store: PostgresNoteStore, total: int, repeats: int) -> dict[str, Any]:
    """Latency of each graph question over `total` notes, asked about random notes."""
    rng = random.Random(1)
    ids = [f"bench-{rng.randrange(total):06d}" for _ in range(repeats)]
    retired = [f"bench-{i:06d}" for i in range(0, total - 1, 50)][:repeats]
    rare = ["bench-000777"] * max(3, repeats // 5)
    common = ["suzuki"] * max(3, repeats // 5)
    results: dict[str, Any] = {}
    for hops in (1, 2, 3):
        results[f"neighborhood_{hops}"] = await _timed(partial(store.neighborhood, hops=hops), ids)
    results["related"] = await _timed(partial(store.related, rel="cites"), ids)
    results["get"] = await _timed(store.get, ids)
    results["edges"] = await _timed(store.edges, ids)
    results["current_successor"] = await _timed(
        partial(store.current_successor, as_of=_AS_OF), retired
    )
    results["search_rare_term"] = await _timed(partial(_search, store), rare)
    results["search_common_term"] = await _timed(partial(_search, store), common)
    results["signature"] = await _timed(lambda _: store.signature(), ids)
    return results


async def _search(store: PostgresNoteStore, term: str) -> object:
    return await store.search([term], _AS_OF)


def _rss_in_a_new_process(dsn: str) -> dict[str, float]:
    """Resident memory a fresh process gains by building the store and asking it things."""
    environment = {**os.environ, "CHEMCLAW_POSTGRES_DSN": dsn}
    completed = subprocess.run(
        [sys.executable, "-m", "chemclaw.cli.kg_store_bench", "--rss-child"],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    if completed.returncode:
        raise RuntimeError(f"the memory probe failed:\n{completed.stderr[-2000:]}")
    return {key: float(value) for key, value in json.loads(completed.stdout).items()}


async def _rss_child() -> None:
    """Print resident memory before the store, after it answered a read, and after a search.

    The search is last and separate: its answer is every note that matches, so a common term
    holds the corpus in memory for as long as the caller holds the result.
    """
    before = _rss_mb()
    async with pooling():
        store = PostgresNoteStore()
        await store.get("bench-000001")
        with contextlib.suppress(KeyError):  # an empty corpus defines no note
            await store.neighborhood("bench-000002", 2)
        after_reads = _rss_mb()
        await store.search(["suzuki"], _AS_OF)
    print(
        json.dumps(
            {
                "before_mb": round(before, 1),
                "after_reads_mb": round(after_reads, 1),
                "after_a_search_matching_every_note_mb": round(_rss_mb(), 1),
            }
        )
    )


async def _run(notes: int, write_notes: int, writers: int, repeats: int) -> dict[str, Any]:
    schema = f"kg_bench_{uuid4().hex[:10]}"
    base = settings.postgres_dsn
    async with connection(base) as conn:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.commit()
    dsn = _scratch_dsn(base, schema)
    settings.postgres_dsn = dsn
    settings.postgres_migration_dsn = dsn
    report: dict[str, Any] = {"notes": notes, "writers": writers}
    try:
        await migrate()
        rng = random.Random(0)
        async with pooling():
            store = PostgresNoteStore()
            report["rss_mb_empty_corpus"] = _rss_in_a_new_process(dsn)
            sample = [_note(i, write_notes, rng) for i in range(write_notes)]
            report["notes_per_second"] = {
                "one_writer": round(await _write(store, sample[: write_notes // 4], 1), 1),
                f"{writers}_writers": round(
                    await _write(store, sample[write_notes // 4 :], writers), 1
                ),
                f"{writers}_writers_with_a_shared_dependency": round(
                    await _write_with_dependency(
                        store,
                        [
                            _note(write_notes + i, write_notes * 2, rng)
                            for i in range(write_notes // 2)
                        ],
                        writers,
                    ),
                    1,
                ),
            }
            loaded = write_notes + write_notes // 2
            started = time.perf_counter()
            await _write(store, [_note(i, notes, rng) for i in range(loaded, notes)], writers)
            report["load_seconds"] = round(time.perf_counter() - started, 1)
            async with connection(dsn) as conn:
                await conn.execute("ANALYZE kg_notes")
                await conn.execute("ANALYZE kg_edges")
                await conn.commit()
            report["latency"] = await _queries(store, notes, repeats)
        report["rss_mb_loaded_corpus"] = _rss_in_a_new_process(dsn)
    finally:
        settings.postgres_dsn = base
        async with connection(base) as conn:
            await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
            await conn.commit()
    return report


def main() -> None:
    """Parse the arguments and print the report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notes", type=int, default=20_000, help="corpus size for the queries")
    parser.add_argument("--write-notes", type=int, default=2_000, help="notes per throughput run")
    parser.add_argument("--writers", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=50, help="questions per latency figure")
    parser.add_argument("--rss-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.rss_child:
        asyncio.run(_rss_child())
        return
    print(json.dumps(asyncio.run(_run(args.notes, args.write_notes, args.writers, args.repeats))))


if __name__ == "__main__":
    main()
