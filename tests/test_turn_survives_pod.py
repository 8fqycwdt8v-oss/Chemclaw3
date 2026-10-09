"""A turn whose pod is killed between two steps is resumed from its checkpoint, once, as its sender.

`D-2026-10-09-a-turn-whose-pod-died-resumes-until-it-has-acted`. Real front-door processes serve the
real graph (`tests/replica_graph.py`: the Postgres checkpointer, the middleware chain, a model that
is a function of the thread) on one database. A process is SIGKILLed at a named point, its claim
lapses, and the sender's attach on another replica is driven at the routes a client calls.

Every claim has its negative control beside it: the read that is repeated against the act that is
not, the sender against the member, the killed turn against the stopped one.
"""

import asyncio
import json
import threading
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from chemclaw.core.config import settings
from tests.pg import create_checkpoint_tables, migrated_db_or_skip
from tests.replicas import Replica, replica, replica_env, replicas

#: The claim lease of the replicas: a killed turn is dead for the others after this long.
LEASE = 3.0
ANA = {"x-test-user": "ana"}
BEN = {"x-test-user": "ben"}
FINAL = "final after {} tool results"


@pytest.fixture(scope="module")
def schema() -> Iterator[None]:
    """A migrated test schema with the checkpointer's tables, reachable from the test process."""
    patch = pytest.MonkeyPatch()
    patch.setattr(settings, "session_store", "postgres")
    asyncio.run(migrated_db_or_skip())
    asyncio.run(create_checkpoint_tables())
    yield
    patch.undo()


@pytest.fixture(scope="module")
def work(schema: None, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The marks and gates the replicas share."""
    path = tmp_path_factory.mktemp("survives")
    (path / "gate").mkdir()
    return path


@pytest.fixture(scope="module")
def env(work: Path) -> dict[str, str]:
    """A replica on the real graph, with a short lease and a budget that books."""
    return replica_env(
        work,
        REPLICA_GRAPH="real",
        service_turn_claim_lease_seconds=LEASE,
        service_turn_relay_lease_seconds=LEASE,
        harness_autonomy="execute",
        budget_enabled="true",
        budget_max_turns_per_user=100,
    )


@pytest.fixture(scope="module")
def survivors(env: dict[str, str]) -> Iterator[list[Replica]]:
    """Two replicas that outlive the tests; each test brings its own victim."""
    with replicas(env, 2) as started:
        yield started


def _rows(statement: str, *params: Any) -> list[tuple[Any, ...]]:
    """Read from the test schema, from outside every replica."""
    with psycopg.connect(settings.postgres_dsn) as conn:
        return conn.execute(statement, params).fetchall()


def _executions(work: Path, tag: str) -> Counter[str]:
    """How many times each model call and tool body of turn `tag` ran, in every process."""
    return Counter(path.name.split(".")[1] for path in (work / "marks").glob(f"{tag}.*"))


def _open_gate(work: Path, tag: str) -> None:
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
        while _executions(self.work, self.tag)[mark] < 1:
            assert time.monotonic() < deadline, f"never reached {mark}:\n{self.server.output()}"
            time.sleep(0.05)

    def kill(self, *, keep_parked: bool = False) -> None:
        """SIGKILL the process, wait until its claim has lapsed for everybody else, open the gate.

        The gate is opened because a resumed turn runs the same message and would park at the same
        place; `keep_parked` leaves it shut for a test that kills the resumed run there too.
        """
        self.server.kill()
        if self.thread is not None:
            self.thread.join(timeout=30)
        time.sleep(LEASE + 1.5)
        if not keep_parked:
            _open_gate(self.work, self.tag)

    def dies_at(self, plan: str, mark: str, *, keep_parked: bool = False) -> "Victim":
        """Run `plan` to `mark`, kill the process there, and return once the claim has lapsed."""
        self.start(plan)
        self.reached(mark)
        self.kill(keep_parked=keep_parked)
        return self


def attach(
    server: Replica, session_id: str, user: dict[str, str]
) -> tuple[int, httpx.Headers, list[dict[str, Any]]]:
    """`GET .../turn/stream` as a client does, read to its end."""
    events: list[dict[str, Any]] = []
    with httpx.stream(
        "GET", f"{server.base}/sessions/{session_id}/turn/stream", headers=user, timeout=120
    ) as response:
        if response.status_code != 200:
            response.read()
            return response.status_code, response.headers, [{"body": response.text}]
        for line in response.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line.removeprefix("data:")))
        return response.status_code, response.headers, events


def _types(events: list[dict[str, Any]]) -> list[str]:
    return [str(event.get("type")) for event in events]


def _question_status(session_id: str) -> str | None:
    rows = _rows(
        "SELECT turn_status FROM session_messages "
        "WHERE session_id = %s AND turn_status IS NOT NULL ORDER BY id DESC LIMIT 1",
        session_id,
    )
    return None if not rows else str(rows[0][0])


def _answers(session_id: str) -> list[str]:
    """The assistant prose rows of the transcript — the answers the chemist can read."""
    rows = _rows(
        "SELECT message->'data'->>'content' FROM session_messages WHERE session_id = %s "
        "AND message->>'type' = 'ai' ORDER BY id",
        session_id,
    )
    return [str(row[0]) for row in rows if row[0]]


def _costs(correlation: str) -> list[tuple[Any, ...]]:
    return _rows(
        "SELECT outcome, input_tokens, output_tokens, actor FROM turn_costs "
        "WHERE correlation_id = %s",
        correlation,
    )


def test_a_turn_killed_between_two_model_calls_is_resumed_once_by_its_sender(
    work: Path, env: dict[str, str], survivors: list[Replica]
) -> None:
    """The answer is produced once, the spend is booked once, and it runs as the sender."""
    b, _ = survivors
    with replica(env) as a:
        dead = Victim(a, work, ANA, "resume1").dies_at("q steps=read,read park-model-2", "model-2")
    assert _question_status(dead.session_id) == "running", "a resumable turn was marked dead"

    status, headers, events = attach(b, dead.session_id, ANA)
    kinds = _types(events)

    assert status == 200
    assert headers["X-Chemclaw-Turn-Correlation-Id"] == dead.correlation
    assert kinds.count("answer") == 1 and kinds[-1] == "answer", kinds
    assert FINAL.format(2) in events[-1]["text"]
    executed = _executions(work, "resume1")
    assert executed == {
        "model-1": 1,  # A, before the kill
        "model-2": 2,  # A, killed inside it; B, again
        "model-3": 1,  # B, the final answer
        "tool-probe_read-1": 1,  # A: recorded in the checkpoint, not repeated
        "tool-probe_read-2": 1,
    }
    assert _answers(dead.session_id) == [FINAL.format(2)]
    assert _question_status(dead.session_id) == "done"
    # One row, for the whole turn: the call the dead attempt completed is in it (it was paid for
    # and never booked), the call it died inside is not (the provider reported nothing for it).
    assert _costs(dead.correlation) == [("answered", 300, 30, "ana")]
    assert _rows("SELECT turns FROM budget_usage WHERE actor = 'ana'") == [(1,)]


def test_only_the_sender_resumes_the_turn_and_a_second_attach_follows_it(
    work: Path, env: dict[str, str], survivors: list[Replica]
) -> None:
    """A member attaching leaves it alone (control); two attaches of the sender run it once."""
    b, c = survivors
    with replica(env) as a:
        dead = Victim(a, work, ANA, "resume2")
        members = httpx.put(
            f"{a.base}/sessions/{dead.session_id}/members/ben", headers=ANA, timeout=30
        )
        assert members.status_code == 204, members.text
        dead.dies_at("q steps=read,read park-model-2", "model-2")

    # Control: Ben is a participant but did not send the turn. Nothing is resumed, nothing marked.
    status, _, _ = attach(b, dead.session_id, BEN)
    assert status == 404
    assert _executions(work, "resume2")["model-3"] == 0
    assert _question_status(dead.session_id) == "running"

    results: list[tuple[int, httpx.Headers, list[dict[str, Any]]]] = []
    threads = [
        threading.Thread(target=lambda s=s: results.append(attach(s, dead.session_id, ANA)))
        for s in (b, c)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert sorted(status for status, _, _ in results)[0] == 200
    assert any(_types(events)[-1:] == ["answer"] for _, _, events in results)
    assert _executions(work, "resume2")["model-3"] == 1, "the turn ran twice"
    assert _executions(work, "resume2")["tool-probe_read-2"] == 1
    assert _answers(dead.session_id) == [FINAL.format(2)]
    assert len(_costs(dead.correlation)) == 1


def test_a_turn_killed_inside_a_state_changing_call_is_not_repeated_and_says_so(
    work: Path, env: dict[str, str], survivors: list[Replica]
) -> None:
    """An act whose result was not recorded may have happened: the turn ends `interrupted`."""
    b, _ = survivors
    with replica(env) as a:
        dead = Victim(a, work, ANA, "act1").dies_at(
            "q steps=read,act park-probe_act", "tool-probe_act-2"
        )

    status, _, body = attach(b, dead.session_id, ANA)

    assert status == 410 and "turn_interrupted" in body[0]["body"]
    assert _executions(work, "act1")["tool-probe_act-2"] == 1, "the act was repeated"
    assert _executions(work, "act1")["model-3"] == 0
    assert _question_status(dead.session_id) == "interrupted"
    assert [row[0] for row in _costs(dead.correlation)] == ["interrupted"]


def test_a_turn_that_acted_earlier_is_not_resumed_even_between_steps(
    work: Path, env: dict[str, str], survivors: list[Replica]
) -> None:
    """Finished acts are in the checkpoint, but their audit rows and approval died with the pod."""
    b, _ = survivors
    with replica(env) as a:
        dead = Victim(a, work, ANA, "act2").dies_at("q steps=act,read park-model-2", "model-2")

    status, _, _ = attach(b, dead.session_id, ANA)

    assert status == 410
    assert _executions(work, "act2")["tool-probe_act-1"] == 1
    assert _executions(work, "act2")["model-3"] == 0
    assert _question_status(dead.session_id) == "interrupted"


def test_a_read_in_flight_at_the_kill_is_repeated_where_an_act_is_not(
    work: Path, env: dict[str, str], survivors: list[Replica]
) -> None:
    """Control for the test above: the same kill point, a tool that only reads, runs again."""
    b, _ = survivors
    with replica(env) as a:
        dead = Victim(a, work, ANA, "read1").dies_at(
            "q steps=read park-probe_read", "tool-probe_read-1"
        )

    status, _, events = attach(b, dead.session_id, ANA)

    assert status == 200 and _types(events)[-1] == "answer"
    assert _executions(work, "read1")["tool-probe_read-1"] == 2
    assert _answers(dead.session_id) == [FINAL.format(1)]


@pytest.mark.parametrize("stopped", [True, False], ids=["stopped", "killed-control"])
def test_a_stopped_turn_is_never_resumed(
    work: Path, env: dict[str, str], survivors: list[Replica], stopped: bool
) -> None:
    """The user's Stop is final; the same kill without it is resumed (the control)."""
    b, _ = survivors
    tag = f"stop{int(stopped)}"
    with replica(env) as a:
        victim = Victim(a, work, ANA, tag)
        victim.start("q steps=read,read park-model-2")
        victim.reached("model-2")
        if stopped:
            answer = httpx.post(
                f"{a.base}/sessions/{victim.session_id}/turn/stop", headers=ANA, timeout=30
            )
            assert answer.json() == {"stopped": True}, answer.text
        victim.kill()

    status, _, _ = attach(b, victim.session_id, ANA)

    if stopped:
        assert status == 404
        assert _question_status(victim.session_id) == "stopped"
        assert _executions(work, tag)["model-3"] == 0
    else:
        assert status == 200
        assert _executions(work, tag)["model-3"] == 1


def test_a_stop_sent_to_a_dead_turn_ends_it_and_it_is_not_resumed_afterwards(
    work: Path, env: dict[str, str], survivors: list[Replica]
) -> None:
    """The Stop that reached nobody (its holder died) still counts when the lease has lapsed."""
    b, _ = survivors
    with replica(env) as a:
        dead = Victim(a, work, ANA, "stopdead").dies_at("q steps=read,read park-model-2", "model-2")

    stop = httpx.post(f"{b.base}/sessions/{dead.session_id}/turn/stop", headers=ANA, timeout=30)
    status, _, _ = attach(b, dead.session_id, ANA)

    assert stop.status_code == 200 and stop.json() == {"stopped": True}
    assert status == 410
    assert _executions(work, "stopdead")["model-3"] == 0
    assert _question_status(dead.session_id) == "interrupted"


def test_a_new_message_supersedes_a_turn_that_could_have_been_resumed(
    work: Path, env: dict[str, str], survivors: list[Replica]
) -> None:
    """Sending again is the chemist's other choice; the old question ends `interrupted`."""
    b, _ = survivors
    with replica(env) as a:
        dead = Victim(a, work, ANA, "super1").dies_at("q steps=read,read park-model-2", "model-2")

    with httpx.stream(
        "POST",
        f"{b.base}/sessions/{dead.session_id}/messages",
        json={"message": "second steps=read tag=super2"},
        headers=ANA,
        timeout=120,
    ) as response:
        events = [
            json.loads(line.removeprefix("data:"))
            for line in response.iter_lines()
            if line.startswith("data:")
        ]
    status, _, _ = attach(b, dead.session_id, ANA)

    assert events[-1]["type"] == "answer"
    assert status == 404, "nothing is running and the old turn must not come back"
    assert _executions(work, "super1")["model-3"] == 0
    assert [row[0] for row in _costs(dead.correlation)] == ["interrupted"]


def test_a_client_attached_to_a_live_turn_on_another_replica_is_unaffected(
    work: Path, env: dict[str, str], survivors: list[Replica]
) -> None:
    """Following a running turn from B resumes nothing; a finished turn is not followable."""
    b, _ = survivors
    with replica(env) as a:
        live = Victim(a, work, ANA, "live1")
        live.start("q steps=read,read park-model-2")
        live.reached("model-2")
        followed: list[tuple[int, httpx.Headers, list[dict[str, Any]]]] = []
        watcher = threading.Thread(target=lambda: followed.append(attach(b, live.session_id, ANA)))
        watcher.start()
        time.sleep(1.5)  # the follow is in place before the turn goes on
        _open_gate(work, "live1")
        watcher.join(timeout=60)
        assert live.thread is not None
        live.thread.join(timeout=60)

        status, _, events = followed[0]
        assert status == 200 and _types(events)[-1] == "answer"
        executed = _executions(work, "live1")
        assert executed["model-2"] == 1 and executed["model-3"] == 1, executed
        assert len(_costs(live.correlation)) == 1
        # Control: the turn is over; an attach finds nothing to follow and nothing to resume.
        assert attach(b, live.session_id, ANA)[0] == 404
        assert _executions(work, "live1")["model-3"] == 1


def test_a_turn_that_dies_again_is_not_resumed_a_second_time(
    work: Path, env: dict[str, str]
) -> None:
    """One resume per turn: a step that kills its pod must not be offered to every attach."""
    with replicas(env, 3) as (a, b, c):
        dead = Victim(a, work, ANA, "twice").dies_at(
            "q steps=read,read park-model-2", "model-2", keep_parked=True
        )
        # B resumes the turn and dies inside the same step.
        attached = threading.Thread(target=lambda: attach(b, dead.session_id, ANA), daemon=True)
        attached.start()
        deadline = time.monotonic() + 90
        while _executions(work, "twice")["model-2"] < 2:
            assert time.monotonic() < deadline, b.output()
            time.sleep(0.05)
        b.kill()
        attached.join(timeout=30)
        time.sleep(LEASE + 1.5)

        status, _, _ = attach(c, dead.session_id, ANA)

        assert status == 410
        assert _executions(work, "twice")["model-2"] == 2
        assert _executions(work, "twice")["model-3"] == 0
        assert _question_status(dead.session_id) == "interrupted"
