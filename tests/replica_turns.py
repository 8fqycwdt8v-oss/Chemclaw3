"""What a test of a killed turn reads and does: the victim, the attach, the rows it left.

Shared by `tests/test_turn_survives_pod.py` and the multi-replica lane
(`tests/live_replicas/`). A turn runs on a `tests.replicas.Replica` over the real graph of
`tests/replica_graph.py`; the helpers kill it at a named point, attach as its sender elsewhere, and
read the transcript and cost rows from outside every replica.
"""

import asyncio
import json
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import psycopg

from chemclaw.agent.checkpointer import close_checkpointer
from chemclaw.agent.state import turn_config
from chemclaw.core.config import settings
from tests.replicas import Replica

ANA = {"x-test-user": "ana"}
BEN = {"x-test-user": "ben"}
FINAL = "final after {} tool results"


def rows(statement: str, *params: Any) -> list[tuple[Any, ...]]:
    """Read from the test schema, from outside every replica."""
    with psycopg.connect(settings.postgres_dsn) as conn:
        return conn.execute(statement, params).fetchall()


def marks(work: Path, tag: str) -> Counter[str]:
    """Every mark turn `tag` left, in every process."""
    return Counter(path.name.split(".")[1] for path in (work / "marks").glob(f"{tag}.*"))


def executions(work: Path, tag: str) -> Counter[str]:
    """How many times each model call and tool body of turn `tag` ran, in every process."""
    return Counter({k: n for k, n in marks(work, tag).items() if not k.endswith("-woke")})


def open_gate(work: Path, tag: str) -> None:
    """Let everything of turn `tag` that parked go on."""
    (work / "gate" / tag).write_text("")


class Victim:
    """A turn running on a replica that a test kills at a chosen point."""

    def __init__(self, server: Replica, work: Path, user: dict[str, str], tag: str) -> None:
        """Create the session as `user` on `server`; nothing is running yet."""
        self.server, self.work, self.user, self.tag = server, work, user, tag
        self.session_id = httpx.post(f"{server.base}/sessions", headers=user, timeout=30).json()[
            "session_id"
        ]
        self.correlation = ""
        self.events: list[dict[str, Any]] = []
        self.thread: threading.Thread | None = None

    def start(self, plan: str) -> None:
        """Send `plan` as the turn's message and read its stream in the background."""

        def read() -> None:
            try:
                with httpx.stream(
                    "POST",
                    f"{self.server.base}/sessions/{self.session_id}/messages",
                    json={"message": f"{plan} tag={self.tag}"},
                    headers=self.user,
                    timeout=120,
                ) as response:
                    self.correlation = response.headers.get("X-Chemclaw-Correlation-Id", "")
                    for line in response.iter_lines():
                        if line.startswith("data:"):
                            self.events.append(json.loads(line.removeprefix("data:")))
            except httpx.TransportError:
                pass  # the process is killed under the stream; that is the point

        self.thread = threading.Thread(target=read, daemon=True)
        self.thread.start()

    def reached(self, mark: str, seconds: float = 90) -> None:
        """Wait until the turn has executed `mark` (for example `model-2`)."""
        deadline = time.monotonic() + seconds
        while executions(self.work, self.tag)[mark] < 1:
            assert time.monotonic() < deadline, f"never reached {mark}:\n{self.server.output()}"
            time.sleep(0.05)

    def checkpointed(self, tool_results: int, seconds: float = 30) -> None:
        """Wait until the thread's newest checkpoint holds `tool_results` tool results.

        A process killed before its last step reached the database loses that step; waiting on the
        rows, not on a margin of time, is what makes the kill point the test names the one it gets.
        """
        deadline = time.monotonic() + seconds
        while sum(kind == "tool" for kind, _ in latest_thread(self.session_id)) < tool_results:
            assert time.monotonic() < deadline, f"{tool_results} tool results never checkpointed"
            time.sleep(0.1)

    def kill(self, *, keep_parked: bool = False) -> None:
        """SIGKILL the process, wait until its claim has lapsed for everybody else, open the gate.

        The gate is opened because a resumed turn runs the same message and would park at the same
        place; `keep_parked` leaves it shut for a test that kills the resumed run there too.
        """
        self.server.kill()
        if self.thread is not None:
            self.thread.join(timeout=30)
        wait_claim_lapsed(self.session_id)
        if not keep_parked:
            open_gate(self.work, self.tag)

    def dies_at(
        self, plan: str, mark: str, *, keep_parked: bool = False, tool_results: int = 0
    ) -> "Victim":
        """Run `plan` to `mark`, kill the process there, and return once the claim has lapsed.

        `tool_results` is how many tool results must be in the checkpoint before the kill.
        """
        self.start(plan)
        self.reached(mark)
        self.checkpointed(tool_results)
        self.kill(keep_parked=keep_parked)
        return self


def wait_claim_lapsed(session_id: str, seconds: float = 60) -> None:
    """Wait until no live claim stands on the session — a dead holder's lease has run out."""
    deadline = time.monotonic() + seconds
    while rows(
        "SELECT 1 FROM session_turns WHERE session_id = %s AND expires_at > now()", session_id
    ):
        assert time.monotonic() < deadline, "the claim never lapsed"
        time.sleep(0.1)


def attach(
    server: Replica,
    session_id: str,
    user: dict[str, str],
    opened: threading.Event | None = None,
) -> tuple[int, httpx.Headers, list[dict[str, Any]]]:
    """`GET .../turn/stream` as a client does, read to its end; `opened` is set once it answers."""
    events: list[dict[str, Any]] = []
    with httpx.stream(
        "GET", f"{server.base}/sessions/{session_id}/turn/stream", headers=user, timeout=120
    ) as response:
        if opened is not None:
            opened.set()
        if response.status_code != 200:
            response.read()
            return response.status_code, response.headers, [{"body": response.text}]
        for line in response.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line.removeprefix("data:")))
        return response.status_code, response.headers, events


def types_of(events: list[dict[str, Any]]) -> list[str]:
    """The `type` of each event, in order."""
    return [str(event.get("type")) for event in events]


def question_status(session_id: str) -> str | None:
    """The `turn_status` of the session's newest question, or `None` when it has none."""
    found = rows(
        "SELECT turn_status FROM session_messages "
        "WHERE session_id = %s AND turn_status IS NOT NULL ORDER BY id DESC LIMIT 1",
        session_id,
    )
    return None if not found else str(found[0][0])


def answers(session_id: str) -> list[str]:
    """The assistant prose rows of the transcript — the answers the chemist can read."""
    found = rows(
        "SELECT message->'data'->>'content' FROM session_messages WHERE session_id = %s "
        "AND message->>'type' = 'ai' ORDER BY id",
        session_id,
    )
    return [str(row[0]) for row in found if row[0]]


def costs(correlation: str) -> list[tuple[Any, ...]]:
    """The cost rows booked for the turn `correlation`: outcome, tokens in and out, actor."""
    return rows(
        "SELECT outcome, input_tokens, output_tokens, actor FROM turn_costs "
        "WHERE correlation_id = %s",
        correlation,
    )


def latest_thread(session_id: str) -> list[tuple[str, str]]:
    """The thread the next turn loads: the newest checkpoint's messages, as (type, text).

    Empty while the session has no checkpoint.
    """

    async def read() -> list[tuple[str, str]]:
        from chemclaw.agent.checkpointer import checkpointer

        try:
            saver = await checkpointer()
            assert saver is not None
            found = await saver.aget_tuple(turn_config(session_id))  # type: ignore[arg-type]
            if found is None:
                return []
            messages = found.checkpoint["channel_values"]["messages"]
            return [
                (m.type + ("+call" if getattr(m, "tool_calls", None) else ""), str(m.content))
                for m in messages
            ]
        finally:
            await close_checkpointer()

    return asyncio.run(read())
