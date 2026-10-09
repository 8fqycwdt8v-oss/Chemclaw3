"""What a turn owes the record is written before the client is told the turn is over.

A real front-door process answers a turn, the client reads the answer frame, and the process is
SIGKILLed at once: no handler runs, nothing in memory is flushed. The cost row, the durable budget
booking and the transcript must already be in Postgres. The ledger and budget writes are slowed
(`REPLICA_WRITE_DELAY`) so the window between "the answer left the process" and "the bookkeeping
landed" is wide, which is what makes a write scheduled behind the answer lose here every time.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from chemclaw.core.config import settings
from tests.pg import create_checkpoint_tables, migrated_db_or_skip
from tests.replicas import Replica, replica, replica_env


@pytest.fixture
def work(tmp_path: Path) -> Path:
    """A scratch directory for the replica, over a migrated schema."""
    asyncio.run(migrated_db_or_skip())
    asyncio.run(create_checkpoint_tables())
    return tmp_path


def _answer_then_kill(
    r: Replica, user: str, message: str, final: str = "answer"
) -> tuple[str, str]:
    """Run one turn as `user`, SIGKILL the process on the first `final` frame, return the ids.

    Returns the session id and the turn's correlation id.
    """
    headers = {"x-test-user": user}
    session_id = httpx.post(f"{r.base}/sessions", headers=headers, timeout=30).json()["session_id"]
    with httpx.stream(
        "POST",
        f"{r.base}/sessions/{session_id}/messages",
        json={"message": message},
        headers=headers,
        timeout=60,
    ) as response:
        assert response.status_code == 200, response.read()
        correlation = response.headers["X-Chemclaw-Correlation-Id"]
        for line in response.iter_lines():
            if line.startswith("data:") and json.loads(line.removeprefix("data:"))["type"] == final:
                r.kill()  # the client has the answer; the process dies before it can do more
                break
        else:
            pytest.fail(f"the turn never sent a {final} frame:\n{r.output()}")
    return session_id, correlation


def _rows(statement: str, *params: Any) -> list[tuple[Any, ...]]:
    """Read from the test schema, from outside the killed process."""
    with psycopg.connect(settings.postgres_dsn) as conn:
        return conn.execute(statement, params).fetchall()


def test_an_answered_turn_is_fully_booked_when_its_process_is_killed_after_the_answer(
    work: Path,
) -> None:
    """The cost row, the budget booking and the transcript all exist after SIGKILL on the answer."""
    env = replica_env(
        work,
        budget_enabled="true",
        budget_max_turns_per_user=100,
        REPLICA_WRITE_DELAY=0.4,
        REPLICA_TOKENS=25,
    )
    with replica(env) as r:
        session_id, correlation = _answer_then_kill(r, "kill-ana", "what is the yield")

    cost = _rows(
        "SELECT completed, outcome, output_tokens FROM turn_costs WHERE correlation_id = %s",
        correlation,
    )
    budget = _rows("SELECT turns, tokens FROM budget_usage WHERE actor = %s", "kill-ana")
    transcript = _rows(
        "SELECT turn_status FROM session_messages WHERE session_id = %s ORDER BY id", session_id
    )

    found = {"cost": cost, "budget": budget, "transcript": transcript}
    assert found == {
        "cost": [(True, "answered", 25)],
        "budget": [(1, 25)],
        "transcript": [("done",), (None,)],
    }, found


def test_a_failed_turn_is_booked_before_the_client_is_told_it_failed(work: Path) -> None:
    """The same holds for the error frame: the cost row says `errored`, the question `failed`."""
    env = replica_env(
        work, budget_enabled="true", budget_max_turns_per_user=100, REPLICA_WRITE_DELAY=0.4
    )
    with replica(env) as r:
        session_id, correlation = _answer_then_kill(r, "kill-ben", "boom now", final="error")

    cost = _rows("SELECT completed, outcome FROM turn_costs WHERE correlation_id = %s", correlation)
    budget = _rows("SELECT turns FROM budget_usage WHERE actor = %s", "kill-ben")
    question = _rows(
        "SELECT turn_status FROM session_messages WHERE session_id = %s ORDER BY id LIMIT 1",
        session_id,
    )
    assert {"cost": cost, "budget": budget, "question": question} == {
        "cost": [(False, "errored")],
        "budget": [(1,)],
        "question": [("failed",)],
    }
