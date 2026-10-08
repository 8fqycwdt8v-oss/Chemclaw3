"""A single-instance job runs once across workers, and a dead holder's job is taken over.

Real separate processes against real Postgres, because the claim is about backends: a lock is
held by a connection, so only another process (or a killed one) can show it behaves as two pods
and a crashed pod do. Each child is a standalone interpreter dialling the test schema's DSN.
"""

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.job_lock import exclusive_job
from tests.pg import migrated_db_or_skip

_HOLDER = """
import asyncio, sys
from chemclaw.core.job_lock import exclusive_job

async def main() -> None:
    async with exclusive_job(sys.argv[1]) as held:
        print("HELD" if held else "SKIPPED", flush=True)
        if held:
            await asyncio.sleep(float(sys.argv[2]))
    print("DONE", flush=True)

asyncio.run(main())
"""

_REINDEXER = """
import asyncio, sys, time
from chemclaw.retrieval import vector_index

real = vector_index.embed_texts

def slow(texts, **kwargs):
    print(f"EMBEDDING {len(texts)}", flush=True)
    time.sleep(float(sys.argv[2]))
    return real(texts, **kwargs)

vector_index.embed_texts = slow

async def main() -> None:
    count = await vector_index.reindex_exclusively(vector_index.PostgresNoteIndex(), sys.argv[1])
    print(f"DONE {count}", flush=True)

asyncio.run(main())
"""


@pytest.fixture
def postgres_worker_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The environment a second pod would have: the same schema and a durable session store."""
    monkeypatch.setattr(settings, "session_store", "postgres")
    return {
        **os.environ,
        "CHEMCLAW_POSTGRES_DSN": settings.postgres_dsn,
        "CHEMCLAW_SESSION_STORE": "postgres",
        "CHEMCLAW_SESSION_STORE_DSN": "",
    }


async def _spawn(script: str, *args: str, env: dict[str, str]) -> asyncio.subprocess.Process:
    """Start a child interpreter running `script` with `args`, stdout piped."""
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _line(process: asyncio.subprocess.Process, timeout: float = 60.0) -> str:
    """The child's next stdout line, failing with its stderr when it dies instead."""
    assert process.stdout is not None
    raw = await asyncio.wait_for(process.stdout.readline(), timeout)
    if not raw:
        assert process.stderr is not None
        err = (await process.stderr.read()).decode()
        raise AssertionError(f"child exited before printing: {err}")
    return raw.decode().strip()


async def _lines_until_done(process: asyncio.subprocess.Process) -> list[str]:
    """Every remaining stdout line up to the child's exit."""
    out: list[str] = []
    while True:
        line = await _line(process)
        out.append(line)
        if line.startswith("DONE"):
            await asyncio.wait_for(process.wait(), 30)
            return out


async def test_two_workers_contend_and_exactly_one_runs(
    postgres_worker_env: dict[str, str],
) -> None:
    """The second worker skips while the first holds the lock, and a later one runs again."""
    await migrated_db_or_skip()
    first = await _spawn(_HOLDER, "contended", "30", env=postgres_worker_env)
    try:
        assert await _line(first) == "HELD"
        second = await _spawn(_HOLDER, "contended", "0", env=postgres_worker_env)
        assert await _line(second) == "SKIPPED", "a second worker ran a job one already held"
        await asyncio.wait_for(second.wait(), 30)
        # Different jobs do not exclude each other.
        other = await _spawn(_HOLDER, "another-job", "0", env=postgres_worker_env)
        assert await _line(other) == "HELD"
        await asyncio.wait_for(other.wait(), 30)
    finally:
        first.kill()
        await first.wait()

    # The lock is released when the holder is gone, not held for the schema's lifetime.
    deadline = time.monotonic() + 15
    while True:
        async with exclusive_job("contended") as held:
            if held:
                break
        assert time.monotonic() < deadline, "the dead holder's lock was never released"
        await asyncio.sleep(0.2)


async def test_a_killed_holders_job_is_taken_over(postgres_worker_env: dict[str, str]) -> None:
    """SIGKILL mid-job frees the lock with no cleanup, and the next worker holds it."""
    await migrated_db_or_skip()
    holder = await _spawn(_HOLDER, "takeover", "300", env=postgres_worker_env)
    assert await _line(holder) == "HELD"
    async with exclusive_job("takeover") as held:
        assert not held, "the lock was not held while its owner was alive"

    holder.send_signal(signal.SIGKILL)
    await holder.wait()

    deadline = time.monotonic() + 15
    while True:
        async with exclusive_job("takeover") as held:
            if held:
                break
        assert time.monotonic() < deadline, "a killed holder's lock was never released"
        await asyncio.sleep(0.2)


async def test_the_lock_is_scoped_to_the_schema(postgres_worker_env: dict[str, str]) -> None:
    """Two deployments sharing one database do not contend: the key folds in the schema."""
    await migrated_db_or_skip()
    holder = await _spawn(_HOLDER, "scoped", "30", env=postgres_worker_env)
    try:
        assert await _line(holder) == "HELD"
        base = settings.postgres_dsn.split("?", 1)[0]
        # The same name from a connection whose search_path differs is another lock.
        other = {**postgres_worker_env, "CHEMCLAW_POSTGRES_DSN": base}
        elsewhere = await _spawn(_HOLDER, "scoped", "0", env=other)
        assert await _line(elsewhere) == "HELD"
        await asyncio.wait_for(elsewhere.wait(), 30)
    finally:
        holder.kill()
        await holder.wait()


async def test_a_memory_session_store_runs_unlocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """One process needs no cluster lock, and must not need a database to run its job."""
    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(settings, "postgres_dsn", "postgresql://nobody@127.0.0.1:1/none")
    async with exclusive_job("anything") as outer, exclusive_job("anything") as inner:
        assert outer and inner


def _write_notes(directory: Path, count: int) -> None:
    (directory / "reaction").mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (directory / "reaction" / f"reaction-{index}.md").write_text(
            f"---\nid: reaction-{index}\ntype: reaction\n---\n\n# Ester {index}\n\nEster.\n",
            encoding="utf-8",
        )


async def test_two_note_reindexers_embed_each_note_once(
    postgres_worker_env: dict[str, str], tmp_path: Path
) -> None:
    """Two background workers reindexing at once do one pass between them, and a later one is idle.

    The first is held inside its embedding call; the second starts then, so the overlap is
    guaranteed rather than hoped for. Without the lock the second would embed every note again.
    """
    await migrated_db_or_skip()
    notes = tmp_path / "notes"
    _write_notes(notes, 3)
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("DELETE FROM note_index")
        await conn.commit()

    first = await _spawn(_REINDEXER, str(notes), "3", env=postgres_worker_env)
    try:
        assert (await _line(first)).startswith("EMBEDDING 3")
        second = await _spawn(_REINDEXER, str(notes), "0", env=postgres_worker_env)
        assert await _lines_until_done(second) == ["DONE 0"], (
            "a second reindex embedded notes while the first held the lock"
        )
        assert await _lines_until_done(first) == ["DONE 3"]
    finally:
        if first.returncode is None:
            first.kill()
            await first.wait()

    async with await connect(settings.postgres_dsn) as conn:
        cursor = await conn.execute("SELECT count(*) FROM note_index")
        assert (await cursor.fetchone()) == (3,)
    third = await _spawn(_REINDEXER, str(notes), "0", env=postgres_worker_env)
    assert await _lines_until_done(third) == ["DONE 0"]


async def test_a_reindexer_killed_mid_pass_is_finished_by_the_next(
    postgres_worker_env: dict[str, str], tmp_path: Path
) -> None:
    """A worker killed inside its pass leaves the lock free, and the next worker does the pass."""
    await migrated_db_or_skip()
    notes = tmp_path / "notes"
    _write_notes(notes, 2)
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("DELETE FROM note_index")
        await conn.commit()

    doomed = await _spawn(_REINDEXER, str(notes), "300", env=postgres_worker_env)
    assert (await _line(doomed)).startswith("EMBEDDING 2")
    doomed.send_signal(signal.SIGKILL)
    await doomed.wait()

    deadline = time.monotonic() + 15
    while True:
        survivor = await _spawn(_REINDEXER, str(notes), "0", env=postgres_worker_env)
        done = await _lines_until_done(survivor)
        if done[-1] != "DONE 0":
            break
        assert time.monotonic() < deadline, "the killed worker's pass was never taken over"
        await asyncio.sleep(0.2)
    assert done[-1] == "DONE 2"
