"""A turn on one replica is the history of the next turn on another.

Two real front-door processes (`tests/replica_process.py`) on one database. Nothing a session needs
lives in a process: the first turn runs on A and B, which has never seen the session, serves its
transcript; the second turn runs on B and A serves both. Run in separate processes because two
replicas in one process share the module-level stores that this is about.
"""

import asyncio
from pathlib import Path

import httpx
import pytest

from tests.pg import create_checkpoint_tables, migrated_db_or_skip
from tests.replicas import replica_env, replicas


@pytest.fixture
def work(tmp_path: Path) -> Path:
    """A scratch directory for the replicas, over a migrated schema with the checkpoint tables."""
    asyncio.run(migrated_db_or_skip())
    asyncio.run(create_checkpoint_tables())
    return tmp_path


def _turn(base: str, session_id: str, message: str) -> None:
    """Run one turn to its end as Ana."""
    with httpx.stream(
        "POST",
        f"{base}/sessions/{session_id}/messages",
        json={"message": message},
        headers={"x-test-user": "ana"},
        timeout=60,
    ) as response:
        assert response.status_code == 200, response.read()
        for _ in response.iter_lines():
            pass


def _transcript(base: str, session_id: str) -> list[tuple[str, str]]:
    """The session's `(role, text)` rows as this replica serves them."""
    rows = httpx.get(
        f"{base}/sessions/{session_id}/messages", headers={"x-test-user": "ana"}, timeout=30
    ).json()
    return [(row["role"], row["text"]) for row in rows]


def test_a_replica_that_never_saw_a_session_serves_what_another_wrote(work: Path) -> None:
    """B reads A's turn, runs the next one, and A reads both."""
    env = replica_env(work)
    with replicas(env, 2) as (a, b):
        created = httpx.post(f"{a.base}/sessions", headers={"x-test-user": "ana"}, timeout=30)
        session_id = created.json()["session_id"]

        _turn(a.base, session_id, "first from a")
        on_b = _transcript(b.base, session_id)
        _turn(b.base, session_id, "second from b")
        on_a = _transcript(a.base, session_id)

    assert on_b == [("user", "first from a"), ("assistant", "answer to first from a")]
    assert on_a == [
        ("user", "first from a"),
        ("assistant", "answer to first from a"),
        ("user", "second from b"),
        ("assistant", "answer to second from b"),
    ]
