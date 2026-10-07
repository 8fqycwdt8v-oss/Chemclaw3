"""`python -m chemclaw.cli.live_storm` — drive the whole live stack hard, with a mock model.

A real model bounds volume by cost and will not reliably emit an empty function name, an
unparseable argument document, forty parallel calls or a turn with no prose. `cli/mock_llm.py`
makes those a parameter, so this harness asks for them by name.

Nothing here is scored from prose: every verdict resolves to an HTTP status, a row count, a
Temporal workflow state, a declared metric, or an event on a stream written to disk.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import shutil
import statistics
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from httpx_sse import aconnect_sse
from temporalio.client import WorkflowExecutionStatus

from chemclaw.connectors.jobs import build_job_tool, job_workflow_id
from chemclaw.connectors.registry import find_job
from chemclaw.core.config import settings
from chemclaw.core.db import _redact
from chemclaw.core.db import connection as db_connection
from chemclaw.core.logging import configure_logging
from chemclaw.core.markdown import render_table
from chemclaw.core.temporal_client import connect as temporal_connect
from chemclaw.evals.live import decoded_events

logger = logging.getLogger(__name__)

# Where the front door and the mock live during a storm. Dev-only addresses, module constants for
# the same reason `connectors_dev` keeps its port here: no deployment reads them.
FRONT_DOOR = "http://127.0.0.1:8000"
MOCK_STATS = "http://127.0.0.1:8820/__mock/stats"

# The scripts that own this lane. Chaos checks restart processes through them, so a recovered
# process is started exactly as the lane starts it.
_LANE_DIR = Path(__file__).resolve().parents[3] / "infra" / "live"

# Every family this harness plans to run, declared once. `report` compares it against the families
# that produced a finding and names the difference: a pass count says nothing about coverage.
FAMILIES: dict[str, str] = {
    "A": "volume, and the admission cap swept end to end",
    "B": "tool bodies really ran, asked of the audit trail",
    "C": "the same call whole, fragmented, and in parallel",
    "D": "identical durable launches colliding",
    "E": "chaos — disconnects, killed workers, a bounced database, a dead broker",
    "F": "adversarial model output a real model will not produce on request",
    "G": "the front door's own limits, asked for deliberately",
    "H": "pathological data: bad chemistry, impossible arguments, unicode, injection",
}

# The admission caps the sweep restarts the front door at: powers of two around the shipped default
# (8), to find where raising the cap stops improving throughput.
_ADMISSION_CAPS = (2, 4, 8, 16, 32)

# The states a workflow never leaves. Asked for rather than assumed, so a wait ends on the truth it
# found instead of on the truth it wanted — the same set `cli/live_jobs.py` polls against.
_TERMINAL = {
    WorkflowExecutionStatus.COMPLETED,
    WorkflowExecutionStatus.FAILED,
    WorkflowExecutionStatus.CANCELED,
    WorkflowExecutionStatus.TERMINATED,
    WorkflowExecutionStatus.TIMED_OUT,
}


@dataclass
class TurnResult:
    """One turn, as the event stream reported it. The unit every family is counted in.

    Every field is read by some check; a field nobody reads goes wrong unnoticed, so do not add one
    without a check.
    """

    session_id: str = ""
    status: int = 0
    seconds: float = 0.0
    answered: bool = False
    # `tool_call` events against `tool_result` events, not keyed by call id: `ToolCallEvent` carries
    # only the tool name, so keying would merge distinct parallel calls. One call announced many
    # times
    # against one result is the fragmentation defect.
    announced: int = 0
    returned: int = 0
    tools_failed: list[str] = field(default_factory=list)
    # What each tool returned, as previewed: a malformed call coming back as a result is acceptable
    # only
    # if the result says it failed.
    result_previews: list[str] = field(default_factory=list)
    error_code: str | None = None
    transport_error: str | None = None


@dataclass
class Finding:
    """One mechanical observation the storm makes, and whether it is what should have happened."""

    family: str
    name: str
    ok: bool
    observed: str
    detail: str = ""


async def run_turn(client: httpx.AsyncClient, message: str) -> TurnResult:
    """Ask the front door one turn and fold its SSE stream into a result.

    A transport failure is recorded rather than raised, so one dropped connection does not lose a
    storm. The behaviour travels inside `message` as the `[[name]]` selector the mock reads, so
    there
    is one source of truth for the scenario.
    """
    result = TurnResult()
    started = time.monotonic()
    try:
        created = await client.post("/sessions", json={})
        created.raise_for_status()
        result.session_id = str(created.json()["session_id"])

        # `evals.live.decoded_events` is the one wire-format reader. The status is read first: a
        # refused
        # turn has a JSON body, which the reader yields nothing for, and a 429 must be recorded as a
        # status.
        async with aconnect_sse(
            client, "POST", f"/sessions/{result.session_id}/messages", json={"message": message}
        ) as source:
            result.status = source.response.status_code
            if result.status != 200:
                await source.response.aread()
                return result
            async for event in decoded_events(source):
                kind = str(event.get("type", ""))
                if kind == "tool_call":
                    result.announced += 1
                elif kind == "tool_result":
                    result.returned += 1
                    result.result_previews.append(str(event.get("preview", "")))
                elif kind == "tool_failed":
                    result.tools_failed.append(str(event.get("tool", "")))
                elif kind == "answer":
                    result.answered = bool(str(event.get("text", "")).strip())
                elif kind == "error":
                    result.error_code = str(event.get("code", "unknown"))
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        result.transport_error = f"{type(exc).__name__}: {exc}"
    result.seconds = time.monotonic() - started
    return result


async def storm(
    behaviour: str,
    *,
    turns: int,
    concurrency: int,
    timeout: float = 300.0,
    message: str | None = None,
) -> list[TurnResult]:
    """Fire `turns` turns of one behaviour, `concurrency` of them in flight at once.

    Concurrency is offered load, not accepted load: the admission semaphore
    (`service_max_concurrent_turns`) is under test, so this must offer far more than it accepts.

    `message` overrides the turn text where the user's own words are under test (unicode,
    injection).
    It must still contain the behaviour selector, which is asserted: without it the default
    behaviour
    would run and the family would pass having tested nothing.
    """
    if message is not None and f"[[{behaviour}]]" not in message:
        raise ValueError(f"a custom storm message must carry the [[{behaviour}]] selector")
    semaphore = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency + 16, max_keepalive_connections=concurrency)

    async with httpx.AsyncClient(
        base_url=FRONT_DOOR, timeout=httpx.Timeout(timeout), limits=limits, trust_env=False
    ) as client:

        async def one(index: int) -> TurnResult:
            async with semaphore:
                return await run_turn(client, message or f"storm turn {index} [[{behaviour}]]")

        return list(await asyncio.gather(*(one(i) for i in range(turns))))


def _lane(script: str, *args: str, env: Mapping[str, str] | None = None) -> str:
    """Run one of the lane's own scripts, returning its output and failing loudly if it fails.

    Synchronous, so always called through `asyncio.to_thread`: `processes.sh restart` can take tens
    of
    seconds, and blocking the loop would stall the in-flight turns a chaos check observes.
    """
    completed = subprocess.run(
        ["/bin/bash", str(_LANE_DIR / script), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
        timeout=900,
    )
    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode != 0:
        raise RuntimeError(f"{script} {' '.join(args)} failed ({completed.returncode}): {output}")
    return output


async def _scalar(sql: str, params: tuple[Any, ...] = ()) -> Any:
    """One value from the live database, through the application's own connection helper."""
    async with db_connection(settings.postgres_dsn) as conn:
        cursor = await conn.execute(sql, params)
        row = await cursor.fetchone()
        return None if row is None else row[0]


async def mock_requests() -> int:
    """How many requests the mock actually served — the storm's proof no real model was called."""
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        try:
            response = await client.get(MOCK_STATS)
            return int(response.json()["requests"])
        except (httpx.HTTPError, KeyError, ValueError):
            return -1


def percentiles(results: Sequence[TurnResult]) -> tuple[float, float]:
    """p50 and p95 of turn latency, over the turns that actually answered."""
    times = sorted(r.seconds for r in results if r.status == 200)
    if not times:
        return (0.0, 0.0)
    p50 = statistics.median(times)
    p95 = times[min(len(times) - 1, int(len(times) * 0.95))]
    return (p50, p95)


# --------------------------------------------------------------------------- families


async def family_c_shapes() -> list[Finding]:
    """C · the same call delivered whole, fragmented, and in parallel.

    Every `response.function_call_arguments.delta` carries both the name and a fragment, so a reader
    treating "name and arguments" as a complete call would emit one `tool_call` per fragment. One
    event per call is correct; this measures what `graph_stream` puts on the wire.
    """
    findings: list[Finding] = []
    for behaviour, expected in (("c-whole", 1), ("c-fragmented", 1), ("c-parallel", 6)):
        results = await storm(behaviour, turns=3, concurrency=3)
        answered = [r for r in results if r.status == 200 and r.returned]
        mismatched = [r for r in answered if r.announced != r.returned]
        shape = [f"{r.announced}/{r.returned}" for r in answered]
        findings.append(
            Finding(
                family="C",
                name=f"{behaviour}: announcements match results ({expected} expected)",
                ok=bool(answered) and not mismatched,
                observed=f"announced/returned per turn: {shape}",
                detail="an announcement with no matching result is a call the surface invented",
            )
        )
    return findings


async def family_d_durable(sessions: int) -> list[Finding]:
    """D · many sessions launching the *identical* durable payload at the same moment.

    The D-011 guarantee under contention: `k` simultaneous launches of one payload must produce
    exactly
    one `job_records` row and at most one computation's worth of cache rows. Zero is a failure — it
    means the payload was already cached and nothing was tested.

    The payload must be cold, and the mock owns it (it imported the catalogue at lane start), so
    this
    family restarts the mock to get a fresh temperature. This process therefore cannot know the
    workflow id; the verdict asks `job_records` for rows the database stamped after the launch
    began,
    which also covers jobs finishing inside `inline_wait_seconds` (no `job_started` event).
    """
    await asyncio.to_thread(_lane, "processes.sh", "restart", "mock-llm")
    since = await _scalar("select now()")
    before = await _scalar("select count(*) from calculation_results")
    results = await storm("d-collide", turns=sessions, concurrency=sessions)
    after = await _scalar("select count(*) from calculation_results")
    recorded = await _scalar("select count(*) from job_records where completed_at >= %s", (since,))

    # And the run is findable afterwards: `find_past_jobs` reads `job_records` through the agent's
    # own
    # tool, asking what a chemist asks the next morning.
    (listed,) = await storm("d-status", turns=1, concurrency=1)

    ok_turns = sum(1 for r in results if r.status == 200)
    return [
        Finding(
            family="D",
            name="a completed job is findable through find_past_jobs afterwards",
            ok=listed.returned > 0 and any("reaction" in p for p in listed.result_previews),
            observed=f"{listed.returned} tool result(s); result[0]={_first_preview(listed)!r}",
            detail="a job record nothing can read back is an archive with no reader",
        ),
        Finding(
            family="D",
            name=f"{sessions} simultaneous identical launches produce exactly one run",
            ok=recorded == 1,
            observed=f"{recorded} job_records row(s) written across {ok_turns} simultaneous turns",
            detail=(
                "one row means the collision happened and rejoined; zero means it never ran and "
                "this measured nothing; more than one means the idempotency key did not hold"
            ),
        ),
        Finding(
            family="D",
            name="the collision computed at most one result set",
            ok=(after - before) <= 8,
            observed=f"calculation_results {before} → {after} (one cold run writes ~3-6 rows)",
            # Zero is legitimate: the workflow id hashes the whole payload including
            # `temperature_k`, while
            # `calculation_results` is keyed on species and method, so a new temperature can be
            # answered
            # entirely from cached species. This bounds only the recompute; "did anything run" is
            # the
            # `job_records` check above.
            detail="twelve launches must not cost twelve computations; zero is a full cache hit",
        ),
    ]


# Words a tool result uses when it is reporting a refusal rather than data. Deliberately a small,
# explicit list: matching "not" or "no" would call half the corpus an error.
_REFUSAL_WORDS = ("error", "invalid", "failed", "unknown", "cannot", "refus", "missing", "required")


def _first_preview(result: TurnResult) -> str | None:
    """The first tool result's text, short enough for a report row."""
    return result.result_previews[0][:70] if result.result_previews else None


def _completed_without_dying(result: TurnResult) -> bool:
    """The turn reached an end the client can read, rather than hanging or dropping the stream.

    For inputs that are large rather than malformed: the system should absorb them, not refuse.
    """
    return (
        result.status == 200
        and result.transport_error is None
        and (result.answered or result.error_code is not None)
    )


def _bad_call_was_reported(result: TurnResult) -> bool:
    """The turn made the *bad tool call* visible — not merely that the turn ended somehow.

    Every adversarial behaviour emits no prose, so any error code would pass vacuously. Requires a
    `tool_failed` event or a `tool_result` whose text says it refused; a result that looks like
    ordinary data fails.
    """
    if result.status != 200:
        return False
    if result.tools_failed:
        return True
    return any(
        any(word in preview.lower() for word in _REFUSAL_WORDS)
        for preview in result.result_previews
    )


async def family_f_adversarial() -> list[Finding]:
    """F · what a real model will not do on request.

    Every case asserts the turn **says** what went wrong; a silent success, an empty answer with no
    error, or a stream that stops are failures however the system recovers internally.
    """
    cases: list[tuple[str, str, Callable[[TurnResult], bool]]] = [
        (
            "f-malformed-json",
            "an unparseable argument document is reported, not swallowed",
            _bad_call_was_reported,
        ),
        (
            "f-cut-off",
            "a call cut off at the output limit is refused, not run on upstream's completion",
            _bad_call_was_reported,
        ),
        (
            "f-wrong-argument",
            "LOAD-1's own shape is visible rather than counted as a call",
            _bad_call_was_reported,
        ),
        (
            "f-unknown-tool",
            "a tool the system does not have fails loudly",
            _bad_call_was_reported,
        ),
        (
            "f-empty-name",
            "an empty function name (STREAM-1) does not kill the turn silently",
            lambda r: r.status == 200 and (r.answered or r.error_code is not None),
        ),
        (
            "f-huge-arguments",
            "a 100 KB argument document is survived, not refused",
            # Not the refusal predicate: a 100 KB search string is legitimate input, and surviving
            # it (an empty
            # result) is correct.
            _completed_without_dying,
        ),
        (
            "f-call-flood",
            "forty parallel calls in one turn are survived",
            _completed_without_dying,
        ),
        (
            "f-no-text",
            "a turn that writes nothing reports empty_answer",
            lambda r: r.error_code == "empty_answer",
        ),
        (
            "f-http-500",
            # As the provider's failure, retryable — not `internal`.
            "an upstream model outage reaches the asker as the model provider's failure",
            lambda r: r.error_code == "llm_timeout",
        ),
    ]
    findings: list[Finding] = []
    for behaviour, claim, predicate in cases:
        (result,) = await storm(behaviour, turns=1, concurrency=1)
        findings.append(
            Finding(
                family="F",
                name=claim,
                ok=predicate(result),
                observed=(
                    f"HTTP {result.status}, answered={result.answered}, "
                    f"error={result.error_code}, tools_failed={result.tools_failed[:2]}, "
                    f"result[0]={_first_preview(result)!r}"
                ),
                detail=result.transport_error or "",
            )
        )
    return findings


async def family_g_limits() -> list[Finding]:
    """G · the front door's own refusals, asked for deliberately.

    Each case wants a specific refusal code, since "did not crash" and "refused correctly" differ
    and
    only the second tells an operator what to change.
    """
    findings: list[Finding] = []
    async with httpx.AsyncClient(base_url=FRONT_DOOR, timeout=30.0, trust_env=False) as client:
        oversized = "x" * (settings.service_max_message_chars + 1_000)
        created = await client.post("/sessions", json={})
        session_id = str(created.json()["session_id"])
        response = await client.post(
            f"/sessions/{session_id}/messages", json={"message": oversized}
        )
        findings.append(
            Finding(
                family="G",
                name=f"a message over {settings.service_max_message_chars} chars is refused",
                ok=response.status_code in (413, 422),
                observed=f"HTTP {response.status_code}",
            )
        )

        # Event streams are capped per user *and* per pod; the per-user cap is the reachable one.
        streams: list[Any] = []
        codes: list[int] = []
        try:
            for _ in range(settings.service_max_event_streams_per_user + 3):
                ctx = client.stream("GET", f"/sessions/{session_id}/events")
                response = await ctx.__aenter__()
                streams.append((ctx, response))
                codes.append(response.status_code)
        finally:
            for ctx, _ in streams:
                await ctx.__aexit__(None, None, None)
        findings.append(
            Finding(
                family="G",
                name="the per-user event-stream cap refuses with 429",
                ok=429 in codes,
                observed=f"codes {codes}",
            )
        )
    return findings


async def family_b_tool_truth(expect_tools: Sequence[str]) -> list[Finding]:
    """B · did tool *bodies* actually run — asked of the audit trail, not of the turn.

    A turn count never stands in for a tool count: calls can die in argument parsing before any tool
    body runs, which only `audit_events` shows.
    """
    # One turn reaching all three tools, so the audit question is asked of this run rather than of
    # old
    # rows; `gather_evidence` and `expand_note` are otherwise rarely exercised.
    await storm("a-retrieval", turns=1, concurrency=1)

    findings: list[Finding] = []
    for tool in expect_tools:
        count = await _scalar("select count(*) from audit_events where tool = %s", (tool,))
        findings.append(
            Finding(
                family="B",
                name=f"{tool} bodies ran",
                ok=bool(count),
                observed=f"{count} audited call(s)",
            )
        )
    return findings


# The reaction the chaos family kills a worker in the middle of: benzene hydrogenation, whose
# species
# appear in no other payload (so no earlier lane cached it) and which is slow enough to interrupt.
#
# The temperature varies per process so reruns do not rejoin the first run's workflow; 100,000
# values
# on a 10-µK grid give a ~27.8-hour period, and the 300.0 K base keeps the grid disjoint from the
# other harnesses (`tests/test_run_jitter.py`).
_CHAOS_TEMPERATURE_K = 300.0 + (int(time.time()) % 100_000) / 100_000.0
_CHAOS_PAYLOAD: dict[str, Any] = {
    "kind": "reaction",
    "reactants": ["c1ccccc1", "[H][H]", "[H][H]", "[H][H]"],
    "products": ["C1CCCCC1"],
    # `standard`, not `quick`: `quick` finishes in seconds, before the kill lands; `standard` adds
    # Hessians long enough to interrupt.
    "level": "standard",
    "temperature_k": _CHAOS_TEMPERATURE_K,
    "symmetry_numbers": {"c1ccccc1": 12, "[H][H]": 2, "C1CCCCC1": 6},
}


async def _workflow_status(workflow_id: str) -> WorkflowExecutionStatus | None:
    """The broker's own view of a workflow — the only authority on whether it survived the kill."""
    client = await temporal_connect()
    description = await client.get_workflow_handle(workflow_id).describe()
    return description.status


async def _chaos_client_disconnect() -> Finding:
    """E1 · a client that walks away mid-turn must free the session at once, not after the lease.

    `api/routes/turns.py` releases the in-process slot and the durable claim in the stream's
    `finally`, which runs on disconnect. Measured as time until the session accepts a new turn,
    since
    that is what a chemist experiences.
    """
    async with httpx.AsyncClient(base_url=FRONT_DOOR, timeout=60.0, trust_env=False) as client:
        created = await client.post("/sessions", json={})
        created.raise_for_status()
        session_id = str(created.json()["session_id"])

        # `f-slow` thinks for eight seconds, so leaving after the first event leaves a turn that is
        # genuinely still running — the case the lease exists for.
        async with client.stream(
            "POST", f"/sessions/{session_id}/messages", json={"message": "chaos [[f-slow]]"}
        ) as response:
            async for _ in response.aiter_lines():
                break

        started = time.monotonic()
        codes: list[int] = []
        for _ in range(300):
            # `stream`, not `post`: a successful re-POST's SSE body lasts the whole next turn, while
            # the question
            # is when the session stops answering 409, which the status line alone answers.
            async with client.stream(
                "POST",
                f"/sessions/{session_id}/messages",
                json={"message": "after the disconnect [[a-cheap]]"},
            ) as probe:
                codes.append(probe.status_code)
            if codes[-1] != 409:
                break
            await asyncio.sleep(0.2)
        waited = time.monotonic() - started

    lease = settings.service_turn_claim_lease_seconds
    return Finding(
        family="E",
        name="a disconnected session accepts a new turn without waiting out the lease",
        # Five seconds, not a fraction of the lease: an explicit release is an order of magnitude
        # faster
        # than lease expiry, and a lease-relative threshold could pass on a lane with a short lease.
        ok=codes[-1] == 200 and waited < 5.0,
        observed=f"accepted after {waited:.1f}s (lease is {lease}s); status codes {codes[:4]}",
        detail="CHAOS-1 regression: this was 63 s before the claim was released on disconnect",
    )


async def _chaos_worker_killed_mid_job() -> Finding:
    """E2 · SIGKILL the connector worker mid-job; Temporal must still finish the job.

    Kills the process outright and starts a new one, as a pod eviction does (`make live-jobs` only
    tests a SIGSTOP stall). The workflow state at the kill is reported, so a job that had already
    completed cannot pass vacuously.

    A killed worker's activity stays `Started` until `xtb_job_heartbeat_timeout_seconds` expires —
    Temporal has no other liveness signal — so the wait budget is derived from that setting, and the
    recovery latency is part of the result.
    """
    connector, job = find_job("compute_reaction_energy")
    tool = build_job_tool(connector, job)
    params_type = tool.__annotations__["params"]
    workflow_id = job_workflow_id(connector, "compute_reaction_energy", _CHAOS_PAYLOAD)

    launch = asyncio.create_task(
        tool(params_type(**_CHAOS_PAYLOAD), "storm chaos: SIGKILL the connector worker mid-job")
    )
    # Poll for RUNNING and kill at once rather than sleeping a guessed interval; `at_kill` records
    # what was actually true so a vacuous pass is detectable.
    at_kill: WorkflowExecutionStatus | None = None
    for _ in range(100):
        with contextlib.suppress(Exception):  # not started yet reads as "not found"
            at_kill = await _workflow_status(workflow_id)
        if at_kill is not None:
            break
        await asyncio.sleep(0.2)

    await asyncio.to_thread(_lane, "processes.sh", "restart", "worker-calc")
    with contextlib.suppress(Exception):
        await launch

    # The budget is the detection window plus room for the job itself to run twice over, because
    # the retry restarts the activity from its first uncached species.
    budget = settings.xtb_job_heartbeat_timeout_seconds + 600
    killed_at = time.monotonic()
    final: WorkflowExecutionStatus | None = None
    for _ in range(budget):
        final = await _workflow_status(workflow_id)
        if final in _TERMINAL:
            break
        await asyncio.sleep(1.0)
    recovered = time.monotonic() - killed_at
    recorded = await _scalar("select count(*) from job_records where job_id = %s", (workflow_id,))

    interrupted = at_kill == WorkflowExecutionStatus.RUNNING
    return Finding(
        family="E",
        name="a job survives its connector worker being SIGKILLed mid-flight",
        ok=interrupted and final == WorkflowExecutionStatus.COMPLETED and bool(recorded),
        observed=(
            f"at kill: {at_kill.name if at_kill else 'not found'}; "
            f"after restart: {final.name if final else 'never terminal'} "
            f"{recovered:.0f}s later (heartbeat timeout is "
            f"{settings.xtb_job_heartbeat_timeout_seconds}s); job_records rows: {recorded}"
        ),
        detail=(
            "the dead worker is detected by the heartbeat timeout and nothing sooner"
            if interrupted
            else "the job was not still running when the worker died — this proved nothing"
        ),
    )


async def _chaos_postgres_bounce() -> Finding:
    """E3 · restart Postgres under load; the pool must reconnect rather than stay poisoned.

    Turns in flight across the bounce may fail; what must hold is that a fresh turn afterwards works
    without restarting the front door.
    """
    load = asyncio.create_task(storm("a-cheap", turns=24, concurrency=8))
    await asyncio.sleep(1.5)
    await asyncio.to_thread(_lane, "bootstrap.sh", "restart-postgres")
    during = await load
    survived = sum(1 for r in during if r.status == 200 and r.error_code is None)

    started = time.monotonic()
    recovered = False
    for _ in range(60):
        (probe,) = await storm("a-cheap", turns=1, concurrency=1)
        if probe.status == 200 and probe.answered:
            recovered = True
            break
        await asyncio.sleep(1.0)
    waited = time.monotonic() - started

    return Finding(
        family="E",
        name="the front door recovers from a Postgres restart without being restarted itself",
        ok=recovered and waited < 45.0,
        observed=(
            f"{survived}/{len(during)} in-flight turns survived the bounce; "
            f"a fresh turn answered {waited:.1f}s after it"
        ),
        detail="in-flight losses are expected; a pool that never reconnects is not",
    )


async def _chaos_broker_outage() -> Finding:
    """E4 · with no broker, a durable launch must *say so* rather than hang or answer anyway.

    The launch cannot even be confirmed, and the failure must appear on the stream
    (`_bad_call_was_reported`). Whether the turn also produces prose is not scored: that prose is
    the
    mock's fixed script, not the system's behaviour.
    """
    await asyncio.to_thread(_lane, "bootstrap.sh", "stop-temporal")
    try:
        (result,) = await storm("d-collide", turns=1, concurrency=1, timeout=120.0)
    finally:
        await asyncio.to_thread(_lane, "bootstrap.sh", "start-temporal")
        # Whatever died while the broker was gone comes back before anything else is measured.
        await asyncio.to_thread(_lane, "processes.sh", "up")

    return Finding(
        family="E",
        name="a durable launch with no broker reaches the asker as an error, not as an answer",
        ok=_bad_call_was_reported(result),
        observed=(
            f"HTTP {result.status}, answered={result.answered}, error={result.error_code}, "
            f"tools_failed={result.tools_failed[:2]}, result[0]={_first_preview(result)!r}"
        ),
        detail=result.transport_error or "",
    )


async def family_e_chaos() -> list[Finding]:
    """E · break the stack while it is working, and measure what it does about it.

    Ordered by blast radius, least first; the broker outage is last because it alone can leave the
    lane needing a restart.
    """
    findings: list[Finding] = []
    for check in (
        _chaos_client_disconnect,
        _chaos_worker_killed_mid_job,
        _chaos_postgres_bounce,
        _chaos_broker_outage,
    ):
        try:
            findings.append(await check())
        except Exception as exc:
            logger.exception("chaos check %s raised", check.__name__)
            findings.append(
                Finding(
                    family="E",
                    name=check.__name__.removeprefix("_chaos_").replace("_", " "),
                    ok=False,
                    observed=f"the check itself raised {type(exc).__name__}: {exc}",
                )
            )
    return findings


#: The gauge the estimator calibration publishes (`agent/context_budget.py` binds it).
ESTIMATOR_RATIO_GAUGE = "chemclaw_context_estimator_ratio"


def metric_sample(exposition: str, name: str) -> float | None:
    """The value of an unlabelled series in a Prometheus text exposition, or `None` if absent."""
    for line in exposition.splitlines():
        head, _, value = line.partition(" ")
        if head == name and value:
            return float(value.split()[0])
    return None


async def _front_door_gauge(name: str) -> float | None:
    """One gauge as the front door's own `/metrics` reports it — `None` if it cannot say."""
    try:
        async with httpx.AsyncClient(base_url=FRONT_DOOR, timeout=10.0, trust_env=False) as client:
            response = await client.get("/metrics")
    except httpx.HTTPError:
        return None
    return metric_sample(response.text, name) if response.status_code == 200 else None


def _calibration_finding(status: int, billed: int, ratio: float | None) -> Finding:
    """H's calibration check: a billed, request-sized turn left the published ratio above 1.

    Strictly above: 1.0 is the clamp every untightened process reads.
    """
    return Finding(
        family="H",
        name="a request-sized bill drives the estimator ratio above 1",
        ok=status == 200 and billed > 0 and ratio is not None and ratio > 1.0,
        observed=(
            f"turn_costs billed={billed} for this session; "
            f"{ESTIMATOR_RATIO_GAUGE}={'unreadable' if ratio is None else f'{ratio:.3f}'}"
        ),
        detail=(
            "every other behaviour bills a constant, which clamps the ratio to 1.0 and leaves "
            "the tightening branch two budget decisions rest on unexercised by any lane"
        ),
    )


async def family_h_edges() -> list[Finding]:
    """H · data a chemist could plausibly send that nothing in the corpus resembles.

    Each case is verified where damage would show: unicode is read back out of Postgres (the answer
    is
    mock text), and the injection string is checked against the table it names.
    """
    findings: list[Finding] = []

    unicode_text = "咖啡因 · Ω · 🧪 · ünïcødé"
    (uni,) = await storm(
        "h-unicode",
        turns=1,
        concurrency=1,
        message=f"what do we know about {unicode_text} [[h-unicode]]",
    )
    stored = await _scalar(
        "select count(*) from session_messages where session_id = %s and message::text like %s",
        (uni.session_id, f"%{unicode_text}%"),
    )
    findings.append(
        Finding(
            family="H",
            name="unicode survives the round trip through Postgres",
            ok=uni.status == 200 and bool(stored),
            observed=(
                f"{stored} session_messages row(s) hold the exact string; answered={uni.answered}"
            ),
        )
    )

    # Drive the estimator calibration. Other behaviours bill a constant 900 tokens against a much
    # larger
    # estimate, which `_Calibration._SANE` drops; `h-size-billed` bills 0.5 tokens per character
    # (about
    # twice the chars/4 estimate), exercising the tightening branch. Asserted on
    # `chemclaw_context_estimator_ratio` (`agent/context_budget.estimator_ratio`), not on
    # `turn_costs.estimated_tokens`, which is 0 for any completed turn.
    (sized,) = await storm("h-size-billed", turns=1, concurrency=1)
    billed = await _scalar(
        "select coalesce(sum(input_tokens), 0) from turn_costs where session_id = %s",
        (sized.session_id,),
    )
    ratio = await _front_door_gauge(ESTIMATOR_RATIO_GAUGE)
    findings.append(_calibration_finding(sized.status, billed, ratio))

    # Only an oversize request reaches `_is_context_length` (per-behaviour `http_status` injection
    # classifies as `error`). The turn must still end where a client can read it.
    (oversize,) = await storm("h-oversize", turns=1, concurrency=1)
    findings.append(
        Finding(
            family="H",
            name="an endpoint refusing an oversize request is classified, not just failed",
            ok=_completed_without_dying(oversize),
            observed=(
                f"HTTP {oversize.status}, answered={oversize.answered}, error={oversize.error_code}"
            ),
            detail="the label space is rate_limited/context_length/timeout/transport/auth/error",
        )
    )

    audit_before = await _scalar("select count(*) from audit_events")
    (inj,) = await storm("h-injection", turns=1, concurrency=1)
    audit_after = await _scalar("select count(*) from audit_events")
    findings.append(
        Finding(
            family="H",
            name="an injection string is treated as a search string",
            ok=inj.status == 200 and audit_after >= audit_before,
            observed=f"audit_events {audit_before} → {audit_after} (a dropped table reads as 0)",
            detail="the string asks for `DROP TABLE audit_events`; the row count is the answer",
        )
    )

    (smiles,) = await storm("h-bad-smiles", turns=1, concurrency=1)
    findings.append(
        Finding(
            family="H",
            name="an unparseable reaction SMILES does not kill the turn",
            ok=_completed_without_dying(smiles),
            observed=(
                f"HTTP {smiles.status}, answered={smiles.answered}, error={smiles.error_code}, "
                f"result[0]={_first_preview(smiles)!r}"
            ),
            # Empty rather than an error is the contract: one retriever unable to parse an optional
            # anchor must
            # not lose the others. The conversational search tools are where "could not read that"
            # is reported.
            detail="an unparseable anchor contributes no chunks; the other retrievers still run",
        )
    )

    (impossible,) = await storm("h-impossible-args", turns=1, concurrency=1)
    findings.append(
        Finding(
            family="H",
            name="arguments that parse and cannot be true are refused, not answered",
            ok=_bad_call_was_reported(impossible),
            observed=(
                f"HTTP {impossible.status}, answered={impossible.answered}, "
                f"error={impossible.error_code}, tools_failed={impossible.tools_failed[:2]}, "
                f"result[0]={_first_preview(impossible)!r}"
            ),
            detail="a symmetry map naming species the equation does not contain",
        )
    )
    return findings


async def family_a_admission(
    *, sweep_turns: int, offered: int, repeats: int
) -> tuple[list[Finding], list[dict[str, Any]]]:
    """A · what the admission cap actually buys, swept by restarting the front door at each value.

    The cap is read once at startup into a semaphore, so the front door is restarted per value.
    Throughput means goodput — turns that answered per second — since counting shed turns would make
    refusing look fast.

    Each cap is sampled `repeats` times (median goodput, a fresh restart each time), and the knee is
    judged against the largest within-cap spread of this run rather than a preset threshold. Four
    verdicts: no turn may vanish; the cap must be load-bearing (top goodput above bottom); the knee
    must resolve against the noise; and the noise must be small enough to read.
    """
    rows: list[dict[str, Any]] = []
    try:
        for cap in _ADMISSION_CAPS:
            samples: list[float] = []
            drains: list[float] = []
            accepted = failed = turns = 0
            p50 = p95 = 0.0
            for _ in range(max(repeats, 1)):
                await asyncio.to_thread(
                    _lane,
                    "processes.sh",
                    "restart",
                    "api",
                    env={"CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS": str(cap)},
                )
                started = time.monotonic()
                results = await storm("a-cheap", turns=sweep_turns, concurrency=offered)
                elapsed = time.monotonic() - started
                accepted = sum(1 for r in results if r.status == 200 and r.error_code is None)
                turns, failed = len(results), len(results) - accepted
                p50, p95 = percentiles(results)
                samples.append(accepted / max(elapsed, 0.001))
                drains.append(turns / max(elapsed, 0.001))
                # Restart inside the loop: reusing a warm process would report its agreement with
                # itself as
                # reproducibility.
            rows.append(
                {
                    "cap": cap,
                    "offered": offered,
                    "turns": turns,
                    "accepted": accepted,
                    "failed": failed,
                    "p50": p50,
                    "p95": p95,
                    # Both, side by side, because they disagree and only one of them is throughput.
                    "drain": statistics.median(drains),
                    "goodput": statistics.median(samples),
                    "samples": samples,
                    # The width of this cap's own disagreement, as a fraction of its median. The
                    # knee is only readable at improvements larger than this.
                    "spread": (max(samples) - min(samples)) / max(statistics.median(samples), 1e-9),
                }
            )
            logger.info(
                "cap=%d accepted=%d/%d p50=%.1fs goodput=%.2f/s (spread %.0f%% over %d sample(s))",
                cap,
                accepted,
                turns,
                p50,
                rows[-1]["goodput"],
                rows[-1]["spread"] * 100,
                len(samples),
            )
    finally:
        # Back to the configured default, whatever happened. Leaving the lane on cap=32 would
        # silently change every family after this one.
        await asyncio.to_thread(_lane, "processes.sh", "restart", "api")

    lost = [row for row in rows if row["accepted"] + row["failed"] != row["turns"]]
    findings = [
        Finding(
            family="A",
            name="every offered turn is accounted for at every cap",
            ok=not lost,
            observed=f"{len(rows)} cap(s) swept, {len(lost)} with unaccounted turns",
        ),
        Finding(
            family="A",
            name="the admission cap is load-bearing (goodput rises with it)",
            ok=bool(rows) and rows[-1]["goodput"] > rows[0]["goodput"],
            observed=(
                f"cap {rows[0]['cap']}: {rows[0]['goodput']:.2f} answered/s → "
                f"cap {rows[-1]['cap']}: {rows[-1]['goodput']:.2f} answered/s"
                if rows
                else "no rows"
            ),
            detail="if this is false, service_max_concurrent_turns is not the knob it looks like",
        ),
        Finding(
            family="A",
            name="the sweep's own noise is small enough to read a knee against",
            ok=bool(rows) and noise(rows) <= _MAX_READABLE_NOISE,
            observed=(
                f"largest within-cap spread {noise(rows) * 100:.0f}% "
                f"over {len(rows[0]['samples'])} sample(s) per cap"
                if rows
                else "no rows"
            ),
            detail=(
                "above this, no step in the sweep can be distinguished from a re-run of the same "
                "cap, and any knee reported would be an artefact — raise --sweep-repeats"
            ),
        ),
        Finding(
            family="A",
            name="the sweep resolves the knee rather than running out of range",
            ok=_knee(rows) is not None,
            observed=(
                f"goodput stops improving at cap {_knee(rows)} "
                f"(steps must beat the {noise(rows) * 100:.0f}% noise floor)"
                if _knee(rows) is not None
                else f"no cap in {_ADMISSION_CAPS} stops paying by more than the "
                f"{noise(rows) * 100:.0f}% noise floor — the sweep's top is a limit of the "
                "sweep, not of the system"
            ),
            detail="SCALE-3's actual question: how high is worth setting this",
        ),
    ]
    return findings, rows


# The most within-cap disagreement a sweep may show and still be read for a knee; above it, one
# sample's error exceeds the steps being compared.
_MAX_READABLE_NOISE = 0.15


def noise(rows: Sequence[dict[str, Any]]) -> float:
    """The largest within-cap spread anywhere in the sweep, as a fraction of that cap's median.

    An upper bound on how wrong one sample can be, taken from this run rather than assumed.
    """
    return max((float(row["spread"]) for row in rows), default=0.0)


def _knee(rows: Sequence[dict[str, Any]]) -> int | None:
    """The first cap whose successor buys less improvement than the sweep's own noise floor.

    Judged against `noise(rows)`, since a step smaller than the spread of one cap was not measured.
    Returns None also when the noise exceeds `_MAX_READABLE_NOISE`: a large noise floor would make
    every step qualify and fabricate a knee at the first pair. None means "not known yet".
    """
    floor = noise(rows)
    if floor > _MAX_READABLE_NOISE:
        return None
    for lower, upper in zip(rows, rows[1:], strict=False):
        if upper["goodput"] < lower["goodput"] * (1 + floor):
            return int(lower["cap"])
    return None


def report(
    findings: Sequence[Finding],
    sweep: Sequence[dict[str, Any]],
    notes: dict[str, Any],
    planned: Sequence[str],
) -> str:
    """The run as tables — every row an observation, none of them a paraphrase.

    The coverage table (planned vs. observed families) comes first: a pass count says only that what
    ran, ran clean.
    """
    observed_families = {finding.family for finding in findings}
    missing = [letter for letter in planned if letter not in observed_families]

    lines = ["# Storm — mock-driven stress, chaos and adversarial pass\n"]
    lines.append(f"Front door `{FRONT_DOOR}` · Temporal `{settings.temporal_address}` · ")
    lines.append(f"Postgres `{_redact(settings.postgres_dsn)}`\n")

    for key, value in notes.items():
        lines.append(f"- **{key}**: {value}")
    lines.append("")

    lines.append("## Coverage\n")
    lines.append(f"**{len(planned) - len(missing)}/{len(planned)} planned families ran.**")
    if missing:
        lines.append(
            f"\n**Did not run: {', '.join(missing)}** — every check below is silent "
            "about whatever they would have measured."
        )
    lines.append("")
    lines.append(
        render_table(
            ["family", "what it covers", "checks"],
            [
                [
                    letter,
                    FAMILIES.get(letter, "?"),
                    str(count) if (count := sum(f.family == letter for f in findings)) else "**0**",
                ]
                for letter in planned
            ],
            align="llr",
        )
    )
    lines.append("")

    if sweep:
        lines.append("## A · admission cap swept (SCALE-3)\n")
        lines.append(
            f"Offered load held at {sweep[0]['offered']} concurrent, "
            f"{sweep[0]['turns']} turns per step; the front door restarted at each cap.\n"
        )
        lines.append(
            render_table(
                [
                    "cap",
                    "accepted",
                    "shed/error",
                    "p50 s",
                    "p95 s",
                    "answered/s",
                    "offered drained/s",
                ],
                [
                    [
                        str(row["cap"]),
                        str(row["accepted"]),
                        str(row["failed"]),
                        f"{row['p50']:.1f}",
                        f"{row['p95']:.1f}",
                        f"{row['goodput']:.2f}",
                        f"{row['drain']:.2f}",
                    ]
                    for row in sweep
                ],
                align="rrrrrrr",
            )
        )
        lines.append(
            "\nThe last column is not throughput — it counts a shed turn as a drained one, so "
            "refusing fast reads as going fast. `answered/s` is the measurement."
        )
        lines.append("")

    lines.append("## Findings\n")
    lines.append(
        render_table(
            ["family", "check", "result", "observed"],
            [
                [
                    finding.family,
                    finding.name,
                    "PASS" if finding.ok else "**FAIL**",
                    finding.observed,
                ]
                for finding in findings
            ],
        )
    )
    passed = sum(1 for f in findings if f.ok)
    lines.append(f"\n**{passed}/{len(findings)} checks passed**, over the families that ran.")
    return "\n".join(lines) + "\n"


async def _require_mock_lane() -> None:
    """Fail before any work if the lane is not pointed at the mock model.

    Every family assumes the mock (malformed calls by name, restarting `mock-llm`). `processes.sh`
    starts `mock-llm` only when `CHEMCLAW_LLM_BASE_URL` names it, so its stats endpoint answering is
    the precondition.

    Raises:
        RuntimeError: The mock is not serving, with the setting that would fix it.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.get(MOCK_STATS)
        reachable = response.status_code == 200
    except httpx.HTTPError:
        reachable = False

    if not reachable:
        raise RuntimeError(
            f"the mock model is not answering at {MOCK_STATS}, so this storm would drive a real "
            "model at load — cost, rate limits, and none of the malformed shapes it exists to "
            "test. Bring the lane up pointed at the mock: `make live-up` with no gateway "
            "configured does exactly that — it starts the mock whenever the resolved "
            "`llm_base_url` is the address the mock serves. "
            "Note that `make live-e2e-full-stack` deliberately runs a real model and is a "
            "different lane from this one."
        )


async def run_storm(
    *, sweep_turns: int, offered: int, collide: int, repeats: int, planned: Sequence[str]
) -> tuple[list[Finding], list[dict[str, Any]]]:
    """Run each planned family in turn; return every finding and the admission sweep's rows.

    Destructive families come last (A restarts the front door, E kills processes); B is last of all
    because it reads the audit trail the others wrote.
    """
    await _require_mock_lane()

    findings: list[Finding] = []
    sweep: list[dict[str, Any]] = []
    selected = set(planned)

    if "C" in selected:
        findings.extend(await family_c_shapes())
    if "D" in selected:
        findings.extend(await family_d_durable(collide))
    if "F" in selected:
        findings.extend(await family_f_adversarial())
    if "G" in selected:
        findings.extend(await family_g_limits())
    if "H" in selected:
        findings.extend(await family_h_edges())
    if "A" in selected:
        admission, sweep = await family_a_admission(
            sweep_turns=sweep_turns, offered=offered, repeats=repeats
        )
        findings.extend(admission)
    if "E" in selected:
        findings.extend(await family_e_chaos())
    if "B" in selected:
        findings.extend(await family_b_tool_truth(["find_notes", "gather_evidence", "expand_note"]))
    return findings, sweep


def main(argv: list[str] | None = None) -> int:
    """Run the storm and write its report; exit non-zero if a check failed *or* a family did not.

    A planned family that produced nothing is an error, so the exit code depends on coverage too.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sweep-turns", type=int, default=48, help="turns per admission-cap step")
    parser.add_argument("--offered", type=int, default=48, help="concurrent turns offered per step")
    parser.add_argument("--collide", type=int, default=12, help="simultaneous identical launches")
    parser.add_argument(
        "--sweep-repeats",
        type=int,
        default=3,
        help="samples per admission cap; the knee is read against the spread they show",
    )
    parser.add_argument(
        "--families",
        default="".join(FAMILIES),
        help=f"which families to run, as letters (default every one: {''.join(FAMILIES)})",
    )
    parser.add_argument("--report", type=Path, default=Path("tasks/live-test/storm.md"))
    args = parser.parse_args(argv)

    planned = [letter for letter in args.families.upper() if letter in FAMILIES]
    unknown = sorted(set(args.families.upper()) - set(FAMILIES))
    if unknown:
        parser.error(f"unknown families {unknown}; known: {sorted(FAMILIES)}")

    configure_logging()

    started = time.monotonic()
    findings, sweep = asyncio.run(
        run_storm(
            sweep_turns=args.sweep_turns,
            offered=args.offered,
            collide=args.collide,
            repeats=args.sweep_repeats,
            planned=planned,
        )
    )
    served = asyncio.run(mock_requests())

    ran = {finding.family for finding in findings}
    notes = {
        "families planned / ran": f"{len(planned)} / {len(ran & set(planned))}",
        "mock requests served": served,
        # The gateway this run actually drove; every model call goes through it.
        "model gateway": settings.llm_base_url,
        "wall clock": f"{time.monotonic() - started:.0f} s",
        "disk free": f"{shutil.disk_usage('.').free // 1_000_000_000} GB",
    }
    text = report(findings, sweep, notes, planned)
    print(text)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(text, encoding="utf-8")
    print(f"written to {args.report}")
    return 0 if all(f.ok for f in findings) and set(planned) <= ran else 1


if __name__ == "__main__":
    raise SystemExit(main())
