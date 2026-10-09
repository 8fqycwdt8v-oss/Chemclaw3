"""The multi-replica lane: three front doors, two background and two calc workers, one database.

`make live-replicas`. Real operating-system processes on one migrated schema and a private Temporal
broker, driven at the routes a client calls and at the queues a launcher writes to. Three claims,
each beside a control that must fail the claim's own measure: processes that keep the state per
process (the count comes out multiplied by the process count) or a turn the rule refuses to resume:

1. Limits hold globally: the per-person request budget, the fleet ceiling on concurrent turns and
   the per-person cap on them admit what is configured, not that times three.
2. A killed pod's turn resumes: the sender's attach on another replica continues it once, as the
   sender, and one cost row is booked. The same kill after a state-changing call is not resumed.
3. One calculation miss computes once: eight identical jobs on two calc workers reach the physics
   server once through the claim ledger, and twice without it.

The front doors serve the real compiled graph over a model that is a function of the thread
(`tests/replica_graph.py`); there is no model credential and none is needed.
"""

import asyncio
import json
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from chemclaw.agent.checkpointer import close_checkpointer
from chemclaw.connectors.calc.specs import EnsembleJobSpec
from chemclaw.connectors.calc.workflows import CalcJobWorkflow
from chemclaw.connectors.queues import bundle_queue
from chemclaw.core.config import settings
from tests.pg import create_checkpoint_tables, migrated_db_or_skip
from tests.replica_turns import (
    FINAL,
    Victim,
    answers,
    attach,
    costs,
    executions,
    open_gate,
    question_status,
    rows,
    types_of,
)
from tests.replicas import free_port, replica_env, replicas, workers

#: Front-door replicas, background workers and calc workers: the lane's topology.
FRONT_DOORS = 3
BACKGROUND_WORKERS = 2
CALC_WORKERS = 2

#: The claim lease of the front doors: a killed turn is dead for the others after this long.
LEASE = 5.0
#: What the lane configures, and so what it asserts was admitted.
BURST = 10
FLEET_TURNS = 3
ACTOR_TURNS = 2
#: Eight identical jobs on two workers that each take two at a time, so both workers hold a miss
#: at once while the computation lasts.
JOBS = 8
ACTIVITIES_PER_CALC_WORKER = 2
COMPUTE_SECONDS = 3.0

BACKGROUND_WORKER = "chemclaw.durable.background_worker"
CALC_WORKER = "tests.live_replicas.calc_worker"


def person(name: str) -> dict[str, str]:
    """The header that makes a request `name`'s."""
    return {"x-test-user": name}


# --- the lane's infrastructure -----------------------------------------------------------------


@pytest.fixture(scope="module")
def schema() -> Iterator[None]:
    """A migrated test schema with the checkpointer's tables; the lane fails, not skips, without it.

    A lane that skips when its database is missing reports green over nothing.
    """
    patch = pytest.MonkeyPatch()
    patch.setattr(settings, "session_store", "postgres")
    try:
        asyncio.run(migrated_db_or_skip())
    except pytest.skip.Exception as unreachable:
        pytest.fail(f"the lane needs Postgres: {unreachable}", pytrace=False)
    asyncio.run(create_checkpoint_tables())
    yield
    asyncio.run(close_checkpointer())
    patch.undo()


@pytest.fixture(scope="module")
def temporal() -> Iterator[str]:
    """A private Temporal dev server, as its own process; yields its `host:port`.

    Private so the lane neither needs nor disturbs a broker the machine already runs, and so its
    task queues hold nothing but this lane's work.
    """
    port = free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "tests.live_replicas.temporal_server", str(port)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert server.stdout is not None
        ready = server.stdout.readline().strip()
        if ready != "ready":
            pytest.fail(f"the Temporal dev server did not start (exit {server.poll()})")
        yield f"127.0.0.1:{port}"
    finally:
        server.send_signal(signal.SIGTERM)
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()
        if server.stdout is not None:
            server.stdout.close()


@pytest.fixture(scope="module")
def work(schema: None, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The marks and gates the front doors share."""
    path = tmp_path_factory.mktemp("lane")
    (path / "gate").mkdir()
    return path


def front_door_env(work: Path, store: str) -> dict[str, str]:
    """The environment of a front door: the real graph, every limit set, state in `store`.

    The per-process turn cap is the fleet ceiling and the replica count is declared as one, which
    startup cannot contradict: only a ceiling counted in the database sees three processes.
    """
    return replica_env(
        work,
        REPLICA_GRAPH="real",
        session_store=store,
        service_turn_claim_lease_seconds=LEASE,
        service_turn_relay_lease_seconds=LEASE,
        harness_autonomy="execute",
        budget_enabled="true",
        budget_max_turns_per_user=100,
        service_rate_limit_per_minute=6,
        service_rate_limit_burst=BURST,
        service_turn_admission_timeout_seconds=2,
        service_max_concurrent_turns=FLEET_TURNS,
        service_fleet_max_concurrent_turns=FLEET_TURNS,
        service_max_concurrent_turns_per_actor=ACTOR_TURNS,
    )


def worker_env(work: Path, temporal: str, **overrides: Any) -> dict[str, str]:
    """The environment of a Temporal worker on the lane's schema and broker."""
    return replica_env(
        work, temporal_address=temporal, worker_allow_unauthenticated="true", **overrides
    )


# --- 1. limits hold globally --------------------------------------------------------------------

STORES = [
    pytest.param("memory", id="per-process-control"),
    pytest.param("postgres", id="shared"),
]


async def _requests_admitted(bases: list[str], user: str, count: int) -> list[int]:
    """`count` simultaneous reads as `user`, spread over `bases`; the status of each."""
    async with httpx.AsyncClient(timeout=60) as client:
        responses = await asyncio.gather(
            *(
                client.get(f"{bases[index % len(bases)]}/profiles", headers=person(user))
                for index in range(count)
            )
        )
    return [response.status_code for response in responses]


@pytest.mark.parametrize("store", STORES)
def test_the_request_budget_is_one_budget_across_three_replicas(work: Path, store: str) -> None:
    """Sixty simultaneous requests from one person over three replicas against a burst of ten.

    Per process each replica holds its own bucket and admits the burst: thirty in all. Shared, the
    burst is spent once whichever replica a request lands on, and the rest are told when to retry.
    """
    with replicas(front_door_env(work, store), FRONT_DOORS) as doors:
        statuses = asyncio.run(_requests_admitted([d.base for d in doors], f"rate-{store}", 60))

    admitted = statuses.count(200)
    assert admitted + statuses.count(429) == 60, statuses
    assert admitted == (BURST * FRONT_DOORS if store == "memory" else BURST), f"{store}: {admitted}"


async def _turn(base: str, user: str, tag: str) -> tuple[int, list[dict[str, Any]]]:
    """Open a session as `user` on one replica and run a turn on it that parks in a tool."""
    async with httpx.AsyncClient(base_url=base, timeout=120) as client:
        created = await client.post("/sessions", headers=person(user))
        session_id = created.json()["session_id"]
        events: list[dict[str, Any]] = []
        async with client.stream(
            "POST",
            f"/sessions/{session_id}/messages",
            json={"message": f"q steps=read park-probe_read tag={tag}"},
            headers=person(user),
        ) as response:
            if response.status_code != 200:
                await response.aread()
                return response.status_code, []
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line.removeprefix("data:")))
    return 200, events


def _started(work: Path, tags: list[str]) -> int:
    """How many of the turns named by `tags` have reached their tool, in any process."""
    return sum(executions(work, tag)["tool-probe_read-1"] for tag in tags)


async def _until(predicate: Any, *, seconds: float = 60.0) -> None:
    """Poll until `predicate()`; fail loudly rather than hang."""
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out waiting"
        await asyncio.sleep(0.05)


@pytest.mark.parametrize("store", STORES)
def test_the_turn_ceiling_is_the_deployments_not_each_replicas(work: Path, store: str) -> None:
    """Twelve people start a turn at once over three replicas under a fleet ceiling of three.

    Per process each replica admits three of its four: nine running at once. Shared, three run
    wherever they landed and the other nine are shed with the retryable `at_capacity` frame once
    the admission timeout has passed.
    """
    tags = [f"ceiling_{store}_{index}" for index in range(12)]
    expected = FLEET_TURNS * FRONT_DOORS if store == "memory" else FLEET_TURNS

    async def run(bases: list[str]) -> list[tuple[int, list[dict[str, Any]]]]:
        turns = [
            asyncio.create_task(_turn(bases[index % FRONT_DOORS], f"c-{store}-{index}", tag))
            for index, tag in enumerate(tags)
        ]
        await _until(lambda: _started(work, tags) >= expected)
        await asyncio.sleep(3.5)  # past the admission timeout: every turn that can be shed was
        running = _started(work, tags)
        for tag in tags:
            open_gate(work, tag)
        results = await asyncio.gather(*turns)
        assert running == expected, f"{running} turns ran at once, not {expected}"
        return list(results)

    with replicas(front_door_env(work, store), FRONT_DOORS) as doors:
        results = asyncio.run(run([d.base for d in doors]))

    answered = [events for _, events in results if any(e["type"] == "answer" for e in events)]
    shed = [events for _, events in results if any(e.get("code") == "at_capacity" for e in events)]
    assert len(answered) == expected
    assert len(shed) == 12 - expected
    assert all(next(e for e in s if e.get("code") == "at_capacity")["retryable"] for s in shed)


@pytest.mark.parametrize("store", STORES)
def test_one_persons_turn_cap_counts_every_replica(work: Path, store: str) -> None:
    """One person's third and fourth turn are refused with 429 on whichever replica they reach.

    The cap is two. Per process each replica lets the person run two: four turns over three
    replicas all run. Shared, the count is read across replicas.
    """
    tags = [f"actor_{store}_{index}" for index in range(4)]

    async def run(bases: list[str]) -> list[int]:
        statuses: list[int] = []
        running: list[asyncio.Task[Any]] = []
        for index, tag in enumerate(tags):
            before = _started(work, tags)
            task = asyncio.create_task(_turn(bases[index % FRONT_DOORS], f"p-{store}", tag))
            running.append(task)
            # One at a time, so each turn has registered before the next asks.
            await _until(lambda t=task, n=before: t.done() or _started(work, tags) > n)
            statuses.append(task.result()[0] if task.done() else 200)
        for tag in tags:
            open_gate(work, tag)
        await asyncio.gather(*running)
        return statuses

    with replicas(front_door_env(work, store), FRONT_DOORS) as doors:
        statuses = asyncio.run(run([d.base for d in doors]))

    refused = 0 if store == "memory" else len(tags) - ACTOR_TURNS
    assert statuses == [200] * (len(tags) - refused) + [429] * refused


# --- 2. a killed pod's turn resumes -------------------------------------------------------------


@pytest.fixture(scope="module")
def shared_env(work: Path) -> dict[str, str]:
    """The front doors that share their state, for the tests that kill one."""
    return front_door_env(work, "postgres")


def test_a_killed_pods_turn_resumes_once_on_another_replica(
    work: Path, shared_env: dict[str, str]
) -> None:
    """Replica A is SIGKILLed between two model calls; the sender's attach lands on B and on C.

    Both attaches arrive at once and the turn runs once: one answer, one set of model calls past
    the kill, one cost row, the question `done`. Before any attach nothing has answered and nothing
    is booked, so the answer is the resume's and not something the dead pod left behind.
    """
    ana = person("lane-ana")
    with replicas(shared_env, FRONT_DOORS) as (a, b, c):
        dead = Victim(a, work, ana, "lane_resume").dies_at(
            "q steps=read,read park-model-2", "model-2"
        )
        assert question_status(dead.session_id) == "running", "a resumable turn was marked dead"
        assert answers(dead.session_id) == [] and costs(dead.correlation) == []

        results: list[tuple[int, Any, list[dict[str, Any]]]] = []
        opened = [threading.Event(), threading.Event()]
        threads = [
            threading.Thread(
                target=lambda s=s, o=o: results.append(attach(s, dead.session_id, ana, o))
            )
            for s, o in zip((b, c), opened, strict=True)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

    assert [status for status, _, _ in results] == [200, 200], results
    assert all(types_of(events)[-1:] == ["answer"] for _, _, events in results), results
    assert all(FINAL.format(2) in events[-1]["text"] for _, _, events in results)
    assert executions(work, "lane_resume") == {
        "model-1": 1,  # A, before the kill
        "model-2": 2,  # A, killed inside it; one survivor, again
        "model-3": 1,  # the survivor, the final answer: once, not once per attach
        "tool-probe_read-1": 1,  # recorded in the checkpoint, not repeated
        "tool-probe_read-2": 1,
    }
    assert answers(dead.session_id) == [FINAL.format(2)]
    assert question_status(dead.session_id) == "done"
    assert costs(dead.correlation) == [("answered", 300, 30, "lane-ana")]
    assert rows("SELECT turns FROM budget_usage WHERE actor = 'lane-ana'") == [(1,)]


def test_a_killed_pods_turn_that_acted_is_not_resumed(
    work: Path, shared_env: dict[str, str]
) -> None:
    """Control for the test above: the same kill after a state-changing call is not continued.

    The call may have taken effect, so the sender is told the turn was interrupted; nothing is
    answered and the cost row says `interrupted`. The attach is answered in both cases, which is
    why the test above asserts the answer and the executions rather than the attach.
    """
    ben = person("lane-ben")
    with replicas(shared_env, FRONT_DOORS) as (a, b, _c):
        dead = Victim(a, work, ben, "lane_acted").dies_at(
            "q steps=act,read park-model-2", "model-2"
        )
        status, _, body = attach(b, dead.session_id, ben)

    assert status == 410 and "turn_interrupted" in body[0]["body"]
    assert executions(work, "lane_acted")["model-3"] == 0
    assert answers(dead.session_id) == []
    assert question_status(dead.session_id) == "interrupted"
    assert [row[0] for row in costs(dead.correlation)] == ["interrupted"]


# --- 3. one calc miss computes once -------------------------------------------------------------


def _background_pollers() -> set[str]:
    """The identities of the processes polling the background queue now."""

    async def ask() -> set[str]:
        client = await Client.connect(
            settings.temporal_address, data_converter=pydantic_data_converter
        )
        answer = await client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(
                namespace=client.namespace,
                task_queue=TaskQueue(name=settings.background_task_queue),
                task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
            ),
            timeout=timedelta(seconds=5),
        )
        return {poller.identity for poller in answer.pollers}

    return asyncio.run(ask())


def _await_pollers(count: int, seconds: float = 60.0) -> set[str]:
    """Wait until `count` distinct processes poll the background queue; return who they are."""
    deadline = time.monotonic() + seconds
    while True:
        found = _background_pollers()
        if len(found) >= count:
            return found
        assert time.monotonic() < deadline, f"{len(found)} of {count} background workers polling"
        time.sleep(0.5)


async def _run_jobs(address: str, spec: EnsembleJobSpec) -> list[str]:
    """Start `JOBS` identical calc jobs at once on the calc queue; the summary each returned."""
    client = await Client.connect(address, data_converter=pydantic_data_converter)
    handles = [
        await client.start_workflow(
            CalcJobWorkflow.run,
            spec,
            id=f"lane-calc-{uuid.uuid4().hex}",
            task_queue=bundle_queue("calc"),
            execution_timeout=timedelta(minutes=5),
        )
        for _ in range(JOBS)
    ]
    return [(await handle.result()).summary for handle in handles]


@pytest.mark.parametrize("store", STORES)
def test_one_calculation_miss_is_computed_once_across_workers(
    work: Path, temporal: str, store: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eight identical jobs on two calc workers, beside two background workers, reach physics once.

    Each worker has its own in-process future, so a worker computes its own misses once whatever
    happens: per process that is one computation per worker, two in all. Through the claim ledger
    the second worker waits for the first's result and the total is one.
    """
    queue = f"lane-background-{uuid.uuid4().hex[:8]}"  # a queue no earlier test left pollers on
    monkeypatch.setattr(settings, "temporal_address", temporal)
    monkeypatch.setattr(settings, "background_task_queue", queue)
    log = work / f"calc-{store}.log"
    log.write_text("")
    calc_env = worker_env(
        work,
        temporal,
        session_store=store,
        worker_max_concurrent_activities=ACTIVITIES_PER_CALC_WORKER,
    )
    calc_env.update(LANE_CALC_LOG=str(log), LANE_CALC_HOLD=str(COMPUTE_SECONDS))
    # A molecule per run: a result persisted by the control would answer the shared run from cache.
    spec = EnsembleJobSpec(smiles=("CCCO" if store == "memory" else "CCCCO"))
    background_env = worker_env(
        work, temporal, session_store="postgres", background_task_queue=queue
    )
    with (
        workers(background_env, [BACKGROUND_WORKER] * BACKGROUND_WORKERS),
        workers(calc_env, [CALC_WORKER] * CALC_WORKERS),
    ):
        assert len(_await_pollers(BACKGROUND_WORKERS)) == BACKGROUND_WORKERS
        summaries = asyncio.run(_run_jobs(temporal, spec))

    calls = [line.split() for line in log.read_text().splitlines()]
    computed = [pid for pid, _tool, key in calls if key != "-"]
    asked = {pid for pid, tool, _key in calls if tool == "calculation_key"}
    assert len(summaries) == JOBS and len(set(summaries)) == 1, summaries
    assert len(asked) == CALC_WORKERS, "one worker took every job, which proves nothing"
    assert len(computed) == (CALC_WORKERS if store == "memory" else 1), calls
