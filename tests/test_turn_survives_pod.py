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
import os
import signal
import threading
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from chemclaw.agent.checkpointer import close_checkpointer
from chemclaw.agent.state import turn_config
from chemclaw.core.config import settings
from tests.pg import create_checkpoint_tables, migrated_db_or_skip
from tests.replicas import Replica, replica, replica_env, replicas

#: The claim lease of the replicas: a killed turn is dead for the others after this long.
LEASE = 5.0
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
        _wait_claim_lapsed(self.session_id)
        if not keep_parked:
            _open_gate(self.work, self.tag)

    def dies_at(self, plan: str, mark: str, *, keep_parked: bool = False) -> "Victim":
        """Run `plan` to `mark`, kill the process there, and return once the claim has lapsed."""
        self.start(plan)
        self.reached(mark)
        self.kill(keep_parked=keep_parked)
        return self


def _wait_claim_lapsed(session_id: str, seconds: float = 60) -> None:
    """Wait until no live claim stands on the session — a dead holder's lease has run out."""
    deadline = time.monotonic() + seconds
    while _rows(
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


@pytest.mark.parametrize("same_replica", [False, True], ids=["other-replica", "same-replica"])
def test_only_the_sender_resumes_the_turn_and_a_second_attach_follows_it(
    work: Path, env: dict[str, str], survivors: list[Replica], same_replica: bool
) -> None:
    """A member attaching leaves it alone (control); two attaches of the sender run it once.

    The attach that loses the race follows the winner to its answer, on this replica or another.
    """
    b, c = survivors
    tag = f"resume2{int(same_replica)}"
    with replica(env) as a:
        dead = Victim(a, work, ANA, tag)
        members = httpx.put(
            f"{a.base}/sessions/{dead.session_id}/members/ben", headers=ANA, timeout=30
        )
        assert members.status_code == 204, members.text
        # The gate stays shut, so the resumed turn is still running when the second attach arrives.
        dead.dies_at("q steps=read,read park-model-2", "model-2", keep_parked=True)

    # Control: Ben is a participant but did not send the turn. Nothing is resumed, nothing marked.
    status, _, _ = attach(b, dead.session_id, BEN)
    assert status == 404
    assert _executions(work, tag)["model-3"] == 0
    assert _question_status(dead.session_id) == "running"

    results: list[tuple[int, httpx.Headers, list[dict[str, Any]]]] = []
    opened = [threading.Event(), threading.Event()]
    threads = [
        threading.Thread(target=lambda s=s, o=o: results.append(attach(s, dead.session_id, ANA, o)))
        for s, o in zip((b, b) if same_replica else (b, c), opened, strict=True)
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 60
    while _executions(work, tag)["model-2"] < 2:  # the winner is in the step the dead one was in
        assert time.monotonic() < deadline, "no attach resumed the turn"
        time.sleep(0.05)
    # Both attaches have been answered, so the one that lost the race is following the winner.
    assert all(event.wait(60) for event in opened), "an attach was never answered"
    _open_gate(work, tag)
    for thread in threads:
        thread.join(timeout=120)

    assert [status for status, _, _ in results] == [200, 200], results
    assert all(_types(events)[-1:] == ["answer"] for _, _, events in results), results
    assert _executions(work, tag)["model-3"] == 1, "the turn ran twice"
    assert _executions(work, tag)["tool-probe_read-2"] == 1
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

    # A page being discarded is not a decision to stop; the turn stays there for the reload.
    unload = httpx.post(
        f"{b.base}/sessions/{dead.session_id}/turn/stop",
        params={"reason": "unload"},
        headers=ANA,
        timeout=30,
    )
    assert unload.status_code == 404
    assert _question_status(dead.session_id) == "running"

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
        following = threading.Event()
        watcher = threading.Thread(
            target=lambda: followed.append(attach(b, live.session_id, ANA, following))
        )
        watcher.start()
        assert following.wait(60), "the follow was never answered"
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
        _wait_claim_lapsed(dead.session_id)

        status, _, _ = attach(c, dead.session_id, ANA)

        assert status == 410
        assert _executions(work, "twice")["model-2"] == 2
        assert _executions(work, "twice")["model-3"] == 0
        assert _question_status(dead.session_id) == "interrupted"


def _stall(server: Replica) -> None:
    """Stop the process as a blocked event loop or a frozen container is stopped.

    It keeps its memory and its connections, does nothing, and wakes believing no time has passed.
    """
    os.kill(server.process.pid, signal.SIGSTOP)


def _wake(server: Replica) -> None:
    os.kill(server.process.pid, signal.SIGCONT)


def test_a_pod_that_wakes_after_its_turn_was_resumed_does_not_act_and_books_nothing(
    work: Path, env: dict[str, str], survivors: list[Replica]
) -> None:
    """The old holder is fenced: it stalls past its lease, B resumes, A wakes and goes to act.

    A's model call returns after B has taken the thread; its next step is a state-changing call.
    That call must not run on A (it runs once, on B), A must book and write nothing for the turn,
    and the transcript holds one answer. The control below is the same stall that ends inside the
    lease: nothing is taken over, and A finishes its own turn and acts.
    """
    b, _ = survivors
    with replica(env) as a:
        victim = Victim(a, work, ANA, "fence1")
        victim.start("q steps=read,act park-model-2")
        victim.reached("model-2")
        _stall(a)
        _wait_claim_lapsed(victim.session_id)

        resumed: list[tuple[int, httpx.Headers, list[dict[str, Any]]]] = []
        attaching = threading.Thread(
            target=lambda: resumed.append(attach(b, victim.session_id, ANA)), daemon=True
        )
        attaching.start()
        deadline = time.monotonic() + 60
        while _executions(work, "fence1")["model-2"] < 2:  # B is in the step A is parked in
            assert time.monotonic() < deadline, b.output()
            time.sleep(0.05)
        _open_gate(work, "fence1")
        _wake(a)
        attaching.join(timeout=120)
        assert victim.thread is not None
        victim.thread.join(timeout=60)
        _checkpoints_settled(victim.session_id)  # A has had every chance to write

        status, _, events = resumed[0]
        assert status == 200 and _types(events)[-1] == "answer", events
        executed = _executions(work, "fence1")
        assert executed["tool-probe_act-2"] == 1, f"the state-changing call ran twice: {executed}"
        assert executed["model-3"] == 1, f"both processes drove the thread: {executed}"
        assert _answers(victim.session_id) == [FINAL.format(2)]
        assert _question_status(victim.session_id) == "done"
        assert len(_costs(victim.correlation)) == 1
        assert httpx.get(f"{a.base}/healthz", timeout=10).status_code == 200


@pytest.mark.parametrize("fenced", [True, False], ids=["fenced", "unfenced-control"])
def test_a_pod_stalled_in_a_model_call_cannot_fork_the_thread_after_its_turn_finished(
    work: Path, env: dict[str, str], survivors: list[Replica], fenced: bool
) -> None:
    """The checkpoint a woken pod writes must not become the thread the next turn loads.

    A is stopped inside its second model call; B resumes the turn and finishes it, answer and all;
    A is continued. A's model node completes and LangGraph submits that step's checkpoint from a
    background task, with an id newer than B's last, so the newest checkpoint would be A's fork:
    the thread without B's answer, ending on a call nothing ran. The checkpointer's write holds the
    claim row, so A's write finds none and the newest checkpoint stays B's. The control strips that
    lock from A and shows the fork, so the assertion is about the fence and not about timing.
    The control strips every fence from A (the checkpointer's lock included); the lock alone is
    shown against a held claim in `tests/test_turn_resume.py`, where no timing is involved.
    """
    b, _ = survivors
    tag = f"fork{int(fenced)}"
    a_env = env if fenced else {**env, "REPLICA_NO_FENCE": "1"}
    with replica(a_env) as a:
        victim = Victim(a, work, ANA, tag)
        victim.start("q steps=read,read park-model-2")
        victim.reached("model-2")
        _stall(a)
        _wait_claim_lapsed(victim.session_id)

        resumed: list[tuple[int, httpx.Headers, list[dict[str, Any]]]] = []
        attaching = threading.Thread(
            target=lambda: resumed.append(attach(b, victim.session_id, ANA)), daemon=True
        )
        attaching.start()
        deadline = time.monotonic() + 60
        while _executions(work, tag)["model-2"] < 2:  # B is in the step A is stopped in
            assert time.monotonic() < deadline, b.output()
            time.sleep(0.05)
        _open_gate(work, tag)  # B runs on to its answer while A stays stopped
        attaching.join(timeout=120)
        status, _, events = resumed[0]
        assert status == 200 and _types(events)[-1] == "answer", events
        finished = _latest_thread(victim.session_id)
        assert finished[-1] == ("ai", FINAL.format(2)), finished
        newest = _latest_checkpoint(victim.session_id)

        _wake(a)
        deadline = time.monotonic() + 60
        while _executions(work, tag)["model-2-woke"] < 2:  # A's model call has returned
            assert time.monotonic() < deadline, a.output()
            time.sleep(0.05)
        assert victim.thread is not None
        victim.thread.join(timeout=60)
        _checkpoints_settled(victim.session_id)

        if fenced:
            assert _latest_checkpoint(victim.session_id) == newest, (
                "the woken pod forked the thread"
            )
            assert _latest_thread(victim.session_id) == finished
        else:
            assert _latest_checkpoint(victim.session_id) != newest, "the control did not fork"
            assert _executions(work, tag)["model-3"] == 2, "the control stopped itself"


def test_a_stall_shorter_than_the_lease_is_not_fenced_and_the_turn_finishes(
    work: Path, env: dict[str, str]
) -> None:
    """Control for the fence: nobody took the turn over, so the stalled pod acts and answers."""
    with replica(env) as a:
        victim = Victim(a, work, ANA, "fence0")
        victim.start("q steps=read,act park-model-2")
        victim.reached("model-2")
        _stall(a)
        time.sleep(1.0)
        _open_gate(work, "fence0")
        _wake(a)
        assert victim.thread is not None
        victim.thread.join(timeout=90)

        executed = _executions(work, "fence0")
        assert executed["tool-probe_act-2"] == 1 and executed["model-3"] == 1, executed
        assert _types(victim.events)[-1] == "answer"
        assert _answers(victim.session_id) == [FINAL.format(2)]
        assert [row[0] for row in _costs(victim.correlation)] == ["answered"]


def _latest_thread(session_id: str) -> list[tuple[str, str]]:
    """The thread the next turn loads: the newest checkpoint's messages, as (type, text)."""

    async def read() -> list[tuple[str, str]]:
        from chemclaw.agent.checkpointer import checkpointer

        try:
            saver = await checkpointer()
            assert saver is not None
            found = await saver.aget_tuple(turn_config(session_id))  # type: ignore[arg-type]
            assert found is not None
            messages = found.checkpoint["channel_values"]["messages"]
            return [
                (m.type + ("+call" if getattr(m, "tool_calls", None) else ""), str(m.content))
                for m in messages
            ]
        finally:
            await close_checkpointer()

    return asyncio.run(read())


def _latest_checkpoint(session_id: str) -> str:
    """The id of the newest checkpoint of the thread — the one the next turn loads."""
    return str(
        _rows(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = '' "
            "ORDER BY checkpoint_id DESC LIMIT 1",
            session_id,
        )[0][0]
    )


def _thread_questions(session_id: str) -> list[str]:
    """The chemist's messages in the session's checkpointed thread, as the model will read them."""
    return [text for kind, text in _latest_thread(session_id) if kind == "human"]


def _checkpoints_settled(session_id: str, quiet: float = 1.0) -> None:
    """Wait until the thread's checkpoint rows have stopped changing for `quiet` seconds."""
    seen = None
    since = time.monotonic()
    deadline = since + 60
    while time.monotonic() < deadline:
        now = _rows(
            "SELECT (SELECT count(*) FROM checkpoints WHERE thread_id = %s), "
            "(SELECT count(*) FROM checkpoint_writes WHERE thread_id = %s)",
            session_id,
            session_id,
        )
        if now != seen:
            seen, since = now, time.monotonic()
        elif time.monotonic() - since >= quiet:
            return
        time.sleep(0.1)
    raise AssertionError("the thread's checkpoints never settled")


def test_a_member_presenting_the_senders_correlation_id_does_not_overwrite_the_question(
    work: Path, env: dict[str, str], survivors: list[Replica]
) -> None:
    """The correlation id is a header any client sets; it is not the question's identity.

    Ben sends a message under Ana's turn's correlation id (visible to him in its response header).
    Both questions stay in the thread the model reads. The thread keeps one message per id, which
    `tests/test_turn_resume.py` shows as the control, so the id must be the server's.
    """
    del survivors  # the shared replicas are not needed; the victim's replica serves both
    with replica(env) as a:
        ana = Victim(a, work, ANA, "ow1")
        added = httpx.put(
            f"{a.base}/sessions/{ana.session_id}/members/ben", headers=ANA, timeout=30
        )
        assert added.status_code == 204, added.text
        ana.start("first steps=read park-model-2")
        ana.reached("model-2")
        ben_events: list[dict[str, Any]] = []

        def ben() -> None:
            with httpx.stream(
                "POST",
                f"{a.base}/sessions/{ana.session_id}/messages",
                json={"message": "second steps=read tag=ow2"},
                headers={**BEN, "X-Chemclaw-Correlation-Id": ana.correlation},
                timeout=120,
            ) as response:
                for line in response.iter_lines():
                    if line.startswith("data:"):
                        ben_events.append(json.loads(line.removeprefix("data:")))

        sending = threading.Thread(target=ben, daemon=True)
        sending.start()
        deadline = time.monotonic() + 30
        while not httpx.get(
            f"{a.base}/sessions/{ana.session_id}/queue", headers=ANA, timeout=10
        ).json()["waiting"]:  # Ben's message is in the line behind Ana's running turn
            assert time.monotonic() < deadline, "Ben's message never joined the line"
            time.sleep(0.05)
        _open_gate(work, "ow1")
        assert ana.thread is not None
        ana.thread.join(timeout=60)
        sending.join(timeout=60)

    assert _types(ben_events)[-1] == "answer", ben_events
    asked = _thread_questions(ana.session_id)
    assert [text.split(" steps=")[0] for text in asked] == ["first", "second"], asked
