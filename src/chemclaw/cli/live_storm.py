"""`python -m chemclaw.cli.live_storm` — drive the whole live stack hard, with a mock model.

The live passes so far used a real model, which bounds volume by cost and — the sharper limit —
puts the interesting inputs out of reach. A real model will not reliably emit an empty function
name, a truncated argument document, forty parallel calls or a turn with no prose, and every one of
those has been a live defect in this system. `cli/mock_llm.py` makes them a parameter, so this
harness can ask for them by name.

**It is committed, and that is a decision rather than tidiness.** The 2026-07 load test's harness
was out-of-tree scripts that no longer exist, so every number in `docs/archive/load-test-2026-07.md`
is a rebuild rather than a replay — and that document's own correction is that a harness which can
silently measure the wrong process is worse than none at all. Two of its findings were later
retracted for exactly that reason.

**Nothing here is scored from prose.** Every verdict resolves to an HTTP status, a row count, a
Temporal workflow state, a declared metric, or an event on a stream that was written to disk. That
is the standing correction from D-2026-08-03, and this run is where it costs the most to ignore:
under load, a plausible summary is the cheapest thing in the system to produce.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import shutil
import statistics
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from temporalio.client import WorkflowExecutionStatus

from chemclaw.cli.storm_behaviours import BEHAVIOURS
from chemclaw.connectors.jobs import build_job_tool, job_workflow_id
from chemclaw.connectors.registry import find_job
from chemclaw.core.config import settings
from chemclaw.core.db import _redact
from chemclaw.core.db import connection as db_connection
from chemclaw.core.logging import configure_logging
from chemclaw.core.temporal_client import connect as temporal_connect

logger = logging.getLogger(__name__)

# Where the front door and the mock live during a storm. Dev-only addresses, module constants for
# the same reason `connectors_dev` keeps its port here: no deployment reads them.
FRONT_DOOR = "http://127.0.0.1:8000"
MOCK_STATS = "http://127.0.0.1:8820/__mock/stats"

# The scripts that own this lane. The chaos family restarts things *through them* rather than by
# calling `kill` and `pg_ctl` itself, so a process it brings back is started exactly as the lane
# starts it — a recovery check that restarted a differently-configured replacement would measure
# something the deployment never runs.
_LANE_DIR = Path(__file__).resolve().parents[3] / "infra" / "live"

# Every family this harness plans to run, declared once. `report` compares this against the
# families that actually produced a finding and names the difference.
#
# It exists because the previous version had no such comparison: `run_storm` wired six of the eight
# families the behaviour catalogue described, and the run printed "17/17 checks passed" — true of
# what ran, and silent about the two that did not. A count of passes cannot say anything about
# coverage, so the coverage has to be declared somewhere a run can be checked against.
FAMILIES: dict[str, str] = {
    "A": "volume, and the admission cap swept end to end",
    "B": "tool bodies really ran, asked of the audit trail",
    "C": "the same call whole, fragmented, and in parallel",
    "D": "identical durable launches colliding",
    "E": "chaos — disconnects, killed workers, a bounced database, a dead broker",
    "F": "adversarial model output a real model will not produce on request",
    "G": "the front door's own limits, asked for deliberately",
    "H": "pathological data: bad chemistry, impossible arguments, unicode, injection",
    # The other eight families named five tools between them when this one was added, over a
    # surface of ninety-nine. This one asks every bundle for something, because "the tool path is
    # exercised" over 5% of the surface is LOAD-1's own shape one level up.
    "T": "every advertised tool, called once with arguments it would accept",
    # Not a scenario family — a property of the lane, checked once per run whatever `--families`
    # selected. It is here rather than in the notes because the notes are where the claim it
    # replaces used to sit, unreconciled, beside a bare `ANTHROPIC_API_KEY set: False`.
    "M": "the lane itself: every model call served by the mock, not by a real endpoint",
}

# The admission caps the SCALE-3 sweep restarts the front door at. Powers of two around the
# shipped default (8) — the row that has been open since July asks where throughput stops
# improving, and that cannot be answered by varying *offered* load alone, which is all the
# previous sweep did.
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


# How many turns this process has driven through the front door since `run_storm` reset it. The
# denominator of the zero-live-model proof: the mock's own counter has to be at least this, or some
# turn's model call went somewhere else. Deliberately incremented in `run_turn` and nowhere else,
# so the two families that open their own client (`G`'s stream cap, `E1`'s disconnect) are *not*
# counted — an undercount can only make the reconciliation easier to satisfy, never falsely fail.
_turns_driven = 0


def turns_driven() -> int:
    """Turns this process has asked the front door for since the last reset."""
    return _turns_driven


@dataclass
class TurnResult:
    """One turn, as the event stream reported it. The unit every family is counted in.

    Every field is read by some check. That is not a coincidence to preserve by hand — four
    others (`behaviour`, `tools_called`, `jobs_started`, `event_counts`) were collected here and
    never looked at once, three of them from the first version and one left behind when family D
    moved to asking the database instead of the stream. A record nobody reads is not evidence
    kept in reserve; it is a field that will be wrong for a while before anyone finds out.
    """

    session_id: str = ""
    status: int = 0
    seconds: float = 0.0
    answered: bool = False
    # `tool_call` events against `tool_result` events. The fragmentation question in one number,
    # and deliberately *not* keyed by call id: `ToolCallEvent` carries no id — only the tool name —
    # so keying by it collapsed six distinct parallel calls into one bucket and reported a working
    # stream as broken. Announcements and results are the two counts the stream can actually
    # distinguish, and one call that announces ten times against a single result is exactly the
    # defect this family found.
    announced: int = 0
    returned: int = 0
    tools_failed: list[str] = field(default_factory=list)
    # What each tool actually returned, as the stream previewed it. The adversarial family
    # needs this: a malformed call that comes back as a *result* is only acceptable if the
    # result says it failed, and 'a tool_result arrived' cannot tell those apart.
    result_previews: list[str] = field(default_factory=list)
    # Why each failure failed, beside `tools_failed`'s *what*. Read by family T's dry-run check.
    #
    # It had only the name, and that cost fourteen checks their meaning on the first run of family
    # T: a refusal is *raised*, so `announce_tool_failures` emits `tool_failed` and the error
    # `ToolMessage` beside it is suppressed from the stream on the documented ground that it "is
    # already reported as tool_failed". A check looking for the refusal text in `result_previews`
    # therefore finds nothing, and reported `refused=0 failed=['synthesize_memory']` about a turn
    # whose stream plainly carried `DryRunRefusal: DRY RUN — synthesize_memory changes stored data
    # or starts work, so it was not called`. Measured against the running front door before it was
    # believed, because "the control did not fire" and "the check read the wrong field" look
    # identical from the report and are not remotely the same finding.
    failure_messages: list[str] = field(default_factory=list)
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


async def run_turn(client: httpx.AsyncClient, message: str, *, dry_run: bool = False) -> TurnResult:
    """Ask the front door one turn and fold its SSE stream into a result.

    A transport failure is recorded rather than raised, the discipline `evals/live.run_probe`
    established: a storm of thousands of turns must not lose all of them because one connection
    dropped, and "the front door stopped answering" is itself the finding.

    The behaviour is not a parameter: it travels inside `message` as the `[[name]]` selector the
    mock reads, so passing it separately meant two places could disagree about which scenario a
    turn was. `storm` asserts the selector is present, which is the check that actually matters.

    `dry_run` *is* a parameter, because it is not the model's to set — it rides the request the way
    `api/schemas.MessageRequest` carries it, and family T is what needs it: the expensive half of
    the tool surface is driven on a dry-run turn so every launcher is refused before anything is
    started. That also makes IDEA-4's gate measurable, which nothing else in this lane does.
    """
    global _turns_driven
    _turns_driven += 1
    result = TurnResult()
    started = time.monotonic()
    try:
        created = await client.post("/sessions", json={})
        created.raise_for_status()
        result.session_id = str(created.json()["session_id"])

        async with client.stream(
            "POST",
            f"/sessions/{result.session_id}/messages",
            json={"message": message, "dry_run": dry_run},
        ) as response:
            result.status = response.status_code
            if response.status_code != 200:
                await response.aread()
                return result
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except ValueError:
                    continue
                kind = str(event.get("type", ""))
                if kind == "tool_call":
                    result.announced += 1
                elif kind == "tool_result":
                    result.returned += 1
                    result.result_previews.append(str(event.get("preview", "")))
                elif kind == "tool_failed":
                    result.tools_failed.append(str(event.get("tool", "")))
                    result.failure_messages.append(str(event.get("message", "")))
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
    dry_run: bool = False,
) -> list[TurnResult]:
    """Fire `turns` turns of one behaviour, `concurrency` of them in flight at once.

    Concurrency is the offered load, not the accepted load — the front door's admission semaphore
    (`service_max_concurrent_turns`) is the thing under test in family A, so this must be able to
    offer far more than it will accept.

    `message` overrides the turn text for the families where the *user's own words* are the thing
    under test — family H sends unicode and an injection string, and the only way to prove either
    survived Postgres is to put it in the message that Postgres stores. The behaviour selector must
    still be in it, since that is how the mock chooses what to do, so this asserts rather than
    trusts: a custom message that forgot the selector would silently run the default behaviour and
    the family would pass having tested nothing.
    """
    if message is not None and f"[[{behaviour}]]" not in message:
        raise ValueError(f"a custom storm message must carry the [[{behaviour}]] selector")
    semaphore = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency + 16, max_keepalive_connections=concurrency)

    async with httpx.AsyncClient(
        base_url=FRONT_DOOR, timeout=httpx.Timeout(timeout), limits=limits
    ) as client:

        async def one(index: int) -> TurnResult:
            async with semaphore:
                return await run_turn(
                    client,
                    message or f"storm turn {index} [[{behaviour}]]",
                    dry_run=dry_run,
                )

        return list(await asyncio.gather(*(one(i) for i in range(turns))))


def _lane(script: str, *args: str, env: Mapping[str, str] | None = None) -> str:
    """Run one of the lane's own scripts, returning its output and failing loudly if it fails.

    Synchronous and therefore always called through `asyncio.to_thread`: `processes.sh restart`
    ready-checks everything it starts, which takes tens of seconds on a cold page cache, and
    blocking the event loop for that long would stall the very in-flight turns a chaos check exists
    to observe.
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
    async with httpx.AsyncClient(timeout=10.0) as client:
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
    # Nearest-rank, `ceil(0.95n) - 1`. `int(0.95n)` is one place further along whenever `0.95n` is
    # a whole number, which at 20, 40 or 60 answered turns published the *slowest* turn as p95 —
    # the column an operator reads beside p50 to decide whether a cap is worth its latency.
    rank = -(-len(times) * 95 // 100)
    p95 = times[min(len(times) - 1, max(rank - 1, 0))]
    return (p50, p95)


# --------------------------------------------------------------------------- families


# What each behaviour declares it will emit, read from the catalogue rather than restated here.
# Family C used to carry `("c-parallel", 6)` tuples whose `6` reached only the check's *name*, and
# family F's "forty parallel calls" named a number nothing compared anything to.
_DECLARED_CALLS = {behaviour.name: len(behaviour.calls) for behaviour in BEHAVIOURS}


def declared_calls(behaviour: str) -> int:
    """How many tool calls `storm_behaviours` says this behaviour emits.

    One declaration, so a check cannot print an expected count it does not assert. Raises
    `KeyError` on an unknown name — a check naming a behaviour the mock will never serve is a
    check that would silently grade the default behaviour instead.
    """
    return _DECLARED_CALLS[behaviour]


async def family_c_shapes() -> list[Finding]:
    """C · the same call delivered whole, fragmented, and in parallel.

    The hypothesis, from reading `api/runner_trace.py` against the Responses client rather than
    from running it: every `response.function_call_arguments.delta` carries **both** the name and a
    non-empty fragment, and `ToolCallTrace.feed` treats "name and arguments" as a complete,
    non-streamed call — so it overwrites the accumulated fragments and flushes immediately. If that
    is right, an 8-fragment call emits 8 `tool_call` events each holding a partial document, rather
    than one holding the reassembled JSON.

    One event per call id is correct. Anything else is the defect, and this is the measurement that
    decides which — the `openai_compatible` path has never been exercised live.
    """
    findings: list[Finding] = []
    for behaviour in ("c-whole", "c-fragmented", "c-parallel"):
        # **The expected count is now asserted, not printed.** It reached only the check's name
        # before, so a turn that announced one call and returned one — five of `c-parallel`'s six
        # silently dropped, or `c-fragmented` reassembled into the wrong number of events — read
        # as `1/1` and passed under a heading saying `(6 expected)`. That is the exact defect this
        # family was built to detect, passing this family's own check.
        expected = declared_calls(behaviour)
        results = await storm(behaviour, turns=3, concurrency=3)
        answered = [r for r in results if r.status == 200 and r.returned]
        wrong = [r for r in answered if not _every_call_came_back(r, expected)]
        shape = [f"{r.announced}/{r.returned}" for r in answered]
        findings.append(
            Finding(
                family="C",
                name=f"{behaviour}: exactly {expected} call(s) announced, and all came back",
                ok=len(answered) == len(results) and not wrong,
                observed=f"announced/returned per turn: {shape} ({expected} expected)",
                detail="an announcement with no matching result is a call the surface invented",
            )
        )
    return findings


async def family_d_durable(sessions: int) -> list[Finding]:
    """D · many sessions launching the *identical* durable payload at the same moment.

    The D-011 guarantee under contention: a duplicate launch must rejoin the existing run rather
    than recompute. Measured by what the database did, never by what a summary said — `k`
    simultaneous launches of one payload must produce one `job_records` row and one computation's
    worth of cache rows.

    **The first version of this family passed by measuring nothing, and the report said so in the
    numbers rather than in the verdict**: "0 distinct workflow id(s) announced across 12 turns;
    calculation_results 113 → 113". The collision payload was fixed across runs, so the *second*
    storm against one database found the answer already cached, launched no workflow, and satisfied
    `len(launched) <= 1` with zero — the exact failure `cli/live_jobs.py` documents at length and
    designs `_RUN_TEMPERATURE_K` against. `<= 1` is the tell: a bound that a run doing nothing also
    meets.

    Two things follow from that, and the second was only found by fixing the first.

    **The payload has to be cold, and the process that owns it is the mock.** Giving
    `COLLISION_PAYLOAD` a per-run temperature is not enough, because the mock is a *separate
    process* that imported the catalogue when the lane came up — its temperature is minutes or
    hours older than this process's, and the two disagree. Restarting the mock is what actually
    makes the payload new, so `run_storm` restarts it before any family runs.

    **That restart used to live here, and it zeroed the run's own evidence.** `processes.sh
    restart mock-llm` resets the mock's request counter, and family D is second in the order — so
    the "mock requests served" figure the report published was the count since the middle of the
    run, silently missing family C and this family's own turns. The restart is a precondition of
    the whole storm rather than of this family, so it now happens once, before the counter starts
    mattering.

    **And that means this process cannot know the workflow id**, which is the better design anyway.
    The verdict is asked of `job_records` by *time* — rows the database stamped after the launch
    began, on its own clock — so it is authoritative regardless of what the mock chose and immune
    to a shared-constant drift of exactly the kind that produced the vacuous pass. It also has to
    be: a job finishing inside `inline_wait_seconds` emits no `job_started` event, so the stream was
    never going to see it.

    Stated as **exactly one**, both times. Zero is the failure this family exists to make visible.

    **The soak found the third thing wrong with this family, and it was in the payload rather than
    the check.** 6 of 81 rounds reported "0 job_records row(s) written", spaced ~12 rounds apart.
    Nothing was broken: `_COLLISION_TEMPERATURE_K` had a 719-second period, so a temperature
    recurred every ~12 minutes, `ALLOW_DUPLICATE_FAILED_ONLY` correctly rejoined the completed run
    rather than recomputing it, and no new row was written. Invisible in a single storm and
    unmissable over hours — which is the whole argument for running one. The period is now
    27 hours; `exactly one` stands.
    """
    since = await _scalar("select now()")
    before = await _scalar("select count(*) from calculation_results")
    results = await storm("d-collide", turns=sessions, concurrency=sessions)
    after = await _scalar("select count(*) from calculation_results")
    recorded = await _scalar("select count(*) from job_records where completed_at >= %s", (since,))

    # And the run is findable afterwards, which is the other half of a durable job being durable.
    # `find_past_jobs` reads `job_records` through the agent's own tool, so this asks the question a
    # chemist asks the next morning — "what did we run?" — of the row the collision just wrote,
    # rather than asking the database twice.
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
            # **Zero is a legitimate outcome here, and demanding otherwise was this check being
            # wrong about the system rather than the reverse.** It first asserted `0 < delta`, on
            # the reasoning that a new workflow must compute something. Measured: one new job
            # record, zero new cache rows. Both are right — the workflow id is a hash of the whole
            # payload including `temperature_k`, while `calculation_results` is keyed on the
            # species and method, so a new temperature is a genuinely new *reaction* question
            # answered entirely from cached *species*. That is D-011 working one level down, which
            # is the opposite of the failure the assertion was written to catch.
            #
            # "Did anything run at all" is the row above's question, asked of `job_records` where
            # it has a real answer. This one only bounds the recompute: twelve simultaneous
            # launches must not cost twelve computations.
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

    For inputs that are merely *large* rather than malformed: the right outcome is that the system
    absorbs them and says something, not that it refuses.
    """
    return (
        result.status == 200
        and result.transport_error is None
        and (result.answered or result.error_code is not None)
    )


def _every_call_came_back(result: TurnResult, expected: int) -> bool:
    """The turn dispatched exactly the calls the behaviour declares, and every one returned.

    **`_completed_without_dying` cannot say this, and three checks were leaning on it to.** Every
    behaviour in the storm either writes prose or trips the `empty_answer` guard, so "the turn
    reached an end a client can read" is satisfied by the mock's own script — with or without a
    single tool call having happened. "Forty parallel calls in one turn are survived" would have
    passed a turn that dispatched none, and "a 100 KB argument document is survived" a turn in
    which the document never reached a tool. Those are the LOAD-1 outcome wearing a pass.

    The expected count comes from `declared_calls`, so the number in the finding's name and the
    number the check compares against are the same number.
    """
    return (
        result.status == 200
        and result.transport_error is None
        and result.announced == expected
        and result.returned == expected
    )


def _outage_reached_the_asker(result: TurnResult) -> bool:
    """A model-transport failure was surfaced as *something other than* an empty turn.

    `f-http-500` declares no calls and no prose, so `empty_answer` is what that turn produces
    whether or not the injected 500 was ever surfaced. A predicate accepting any error code was
    therefore satisfied by the behaviour's own emptiness — the same vacuous shape
    `_bad_call_was_reported` exists to remove, one row further down the same table.
    """
    if result.status != 200:
        return True
    return result.error_code is not None and result.error_code != "empty_answer"


def _bad_call_was_reported(result: TurnResult) -> bool:
    """The turn made the *bad tool call* visible — not merely that the turn ended somehow.

    The distinction is the whole value of this family, and the first version did not make it. Every
    adversarial behaviour emits no prose, so every one of them produced `empty_answer`, and a
    predicate that accepted any error code passed all eight without ever looking at the tool. That
    is the vacuous-check pattern this repository has now hit three times in one day — a signal that
    reports success for a reason unrelated to what it claims to measure.

    So: a `tool_failed` event, or a `tool_result` whose text says it refused. A result that came
    back looking like ordinary data is the LOAD-1 outcome and fails here.
    """
    if result.status != 200:
        return False
    if result.tools_failed:
        return True
    return any(
        any(word in preview.lower() for word in _REFUSAL_WORDS)
        for preview in result.result_previews
    )


def _partial_document_was_completed(result: TurnResult) -> bool:
    """The truncated document was closed by the streaming parser and the tool ran on the cut value.

    **This check asserts the behaviour rather than complaining about it, and that is deliberate.**
    Measured 2026-08-28 against `AIMessageChunk`: `'{"text": "unterminated'` arrives as a *valid*
    tool call `{'text': 'unterminated'}` with `invalid_tool_calls` empty, because LangChain runs a
    streamed call's argument fragments through `parse_partial_json`, which closes an unterminated
    string and an unclosed brace. `agent/model_calls.py`'s docstring records the same measurement.

    So `find_notes` searches for a word the model never finished writing and answers "no matches",
    and nothing anywhere says the argument was cut. That is
    `D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` one layer further out than this
    repository has met it before: not a call that vanished, but a call that *ran* on an argument
    its author did not finish. The completion happens inside upstream's streaming assembler, above
    anything `src/` owns, so this is a property to keep checked rather than a defect to patch here.

    The check that used to stand in this slot claimed the opposite — "a truncated argument document
    is reported, not swallowed" — and had been failing for that reason rather than for a defect,
    which is the vacuous-check pattern in reverse: a check that cannot pass because it asks for
    something the system documents it does not do.

    The declared call was announced and came back, and no preview says it refused. **The count is
    the positive half and it was missing**: `not any(refusal word in preview)` is trivially true of
    a turn with no previews at all, and the `bool(result_previews)` guard in front of it is
    satisfied by a single *empty-string* preview — which is what the measured stream really
    carries for `find_notes`. So this scored a turn that announced nothing. That is the shape of
    the UI's `not.toContain('unreachable')` over a body naming no connector, in this file.

    If upstream ever starts rejecting the document instead, this goes red and the finding is that
    the behaviour changed.
    """
    if result.tools_failed:
        return False
    if not _every_call_came_back(result, declared_calls("f-truncated-arguments")):
        return False
    return not any(
        any(word in preview.lower() for word in _REFUSAL_WORDS)
        for preview in result.result_previews
    )


async def family_f_adversarial() -> list[Finding]:
    """F · what a real model will not do on request.

    Every case asserts the same thing in a different disguise: the turn must **say** what went
    wrong. A silent success, an empty answer with no error, or a stream that simply stops are all
    failures here regardless of how the system recovers internally — that is the class this
    repository has now met five times (D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed).
    """
    cases: list[tuple[str, str, Callable[[TurnResult], bool]]] = [
        (
            "f-malformed-json",
            "an unparseable argument document is reported, not swallowed",
            _bad_call_was_reported,
        ),
        (
            "f-truncated-arguments",
            "a truncated argument document is completed and run — the tool sees the cut value",
            _partial_document_was_completed,
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
            "an empty function name (STREAM-1) is reported, not swallowed",
            # The refusal predicate, not "the turn ended somehow": this behaviour writes no prose,
            # so `empty_answer` was guaranteed and the old predicate could not fail. Measured, the
            # stream carries `tool_failed` with an empty tool name, which is the reporting the
            # check's name claims.
            _bad_call_was_reported,
        ),
        (
            "f-huge-arguments",
            "a 100 KB argument document is survived — the call came back",
            # Deliberately *not* the refusal predicate. A 100 KB search string is legitimate input,
            # and the measured behaviour is that `find_notes` ran and returned `[]` — surviving it
            # is the correct outcome, so asserting a refusal was this check being wrong about what
            # good looks like rather than the system being wrong. What it must also say is that
            # the call happened at all, which `_completed_without_dying` could not.
            lambda r: _every_call_came_back(r, declared_calls("f-huge-arguments")),
        ),
        (
            "f-call-flood",
            "forty parallel calls in one turn are survived — all forty came back",
            lambda r: _every_call_came_back(r, declared_calls("f-call-flood")),
        ),
        (
            "f-no-text",
            "a turn that writes nothing reports empty_answer",
            lambda r: r.error_code == "empty_answer",
        ),
        (
            "f-http-500",
            "an upstream model outage reaches the asker as an error of its own",
            _outage_reached_the_asker,
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
                    f"error={result.error_code}, announced={result.announced}/"
                    f"{declared_calls(behaviour)} returned={result.returned}, "
                    f"tools_failed={result.tools_failed[:2]}, "
                    f"result[0]={_first_preview(result)!r}"
                ),
                detail=result.transport_error or "",
            )
        )
    return findings


async def family_g_limits() -> list[Finding]:
    """G · the front door's own refusals, asked for deliberately.

    A limit nobody has ever hit is a limit nobody has tested. Each case wants a *specific* refusal
    code, because "it did not crash" and "it refused correctly" are different outcomes and only one
    of them tells an operator what to change.
    """
    findings: list[Finding] = []
    async with httpx.AsyncClient(base_url=FRONT_DOOR, timeout=30.0) as client:
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


async def _audited_calls(tool: str) -> int:
    """How many calls of one tool the audit trail holds right now."""
    return int(await _scalar("select count(*) from audit_events where tool = %s", (tool,)) or 0)


def _tool_truth_finding(tool: str, before: int, after: int) -> Finding:
    """Did this run put a row in the audit trail for `tool` — as a delta, never as a total.

    The total is the residue of every run this database has ever seen, so `bool(count)` was true
    on an empty run and would stay true if the tool were deleted tomorrow. Only the movement
    across the driving turn is evidence that a body ran *here*.
    """
    ran = after - before
    return Finding(
        family="B",
        name=f"{tool} bodies ran",
        ok=ran > 0,
        observed=f"{ran} audited call(s) during this run ({before} → {after} lifetime)",
        detail="a lifetime count is answered by residue; the delta is the measurement",
    )


async def family_b_tool_truth(expect_tools: Sequence[str]) -> list[Finding]:
    """B · did tool *bodies* actually run — asked of the audit trail, not of the turn.

    LOAD-1 made permanent. The previous load test reported "100 tool calls, the tool path is
    genuinely exercised" while every call had died in MAF's parse-error branch before any tool body
    ran; the truth was only recoverable afterwards from `audit_events`. So a turn count is never
    allowed to stand in for a tool count here.
    """
    # One turn that reaches for all three, so the audit question below is asked of something this
    # run actually did rather than of residue an earlier run left in the table. `find_notes` is
    # exercised by nearly every family; `gather_evidence` and `expand_note` are not, and without
    # this the two of them would be answered by rows of unknown age.
    #
    # **The turn was necessary and was not sufficient, because the query had no time bound.** An
    # unbounded `count(*)` is answered by that residue exactly as before — measured, it reported
    # "366 audited call(s)" for a table this run contributed three of — so a do-nothing run passes
    # it and always would. The counts are read either side of the driving turn and it is the
    # *delta* that is the finding.
    before = {tool: await _audited_calls(tool) for tool in expect_tools}
    await storm("a-retrieval", turns=1, concurrency=1)
    return [
        _tool_truth_finding(tool, before[tool], await _audited_calls(tool)) for tool in expect_tools
    ]


# The reaction the chaos family kills a worker in the middle of. Benzene hydrogenation rather than
# the ammonia synthesis `cli/live_jobs.py` uses, for two independent reasons: its species appear in
# no other payload in this repository, so the first run of a storm cannot be answered from a cache
# some earlier lane filled — and it is *slow enough to interrupt*. A job that finishes before the
# SIGKILL lands would report a passing durability check having tested nothing, which is the vacuous
# pass this harness has now had to correct three times.
#
# The temperature varies per process for the reason `live_jobs._RUN_TEMPERATURE_K` does: the
# workflow id is a hash of the payload, so a fixed one makes every rerun a rejoin of the first.
#
# `% 971` recurred every 16.2 minutes — 1.35x the ~12-minute period that had already been *measured*
# failing this harness (`cli/storm_behaviours.py` records 6 of 81 soak rounds reporting "0
# job_records row(s) written", which was D-011's cache working correctly and being read as a
# failure). Same modulus as the other two copies now: ~27.8 hours, past any soak this harness runs.
_CHAOS_TEMPERATURE_K = 300.0 + (int(time.time()) % 100_000) / 100_000.0
_CHAOS_PAYLOAD: dict[str, Any] = {
    "kind": "reaction",
    "reactants": ["c1ccccc1", "[H][H]", "[H][H]", "[H][H]"],
    "products": ["C1CCCCC1"],
    # `standard`, not `quick`, and that is the difference between a check and a decoration. At
    # `quick` this job optimises and reports ΔE, which for these three species finished in under
    # four seconds — measured: the first run of this check killed the worker with the workflow
    # already COMPLETED and correctly reported that it had proved nothing. `standard` adds the
    # thermochemistry, so there are Hessians on a 12-atom and an 18-atom molecule to interrupt.
    "level": "standard",
    "temperature_k": _CHAOS_TEMPERATURE_K,
    "symmetry_numbers": {"c1ccccc1": 12, "[H][H]": 2, "C1CCCCC1": 6},
}


async def _workflow_status(workflow_id: str) -> WorkflowExecutionStatus | None:
    """The broker's own view of a workflow — the only authority on whether it survived the kill."""
    client = await temporal_connect()
    description = await client.get_workflow_handle(workflow_id).describe()
    return description.status


def _release_was_observed(codes: Sequence[int], *, waited: float) -> bool:
    """The session was actually held, and then let go quickly — both halves, in that order.

    **The first half was missing and it is the same guard E2 and E3 each carry.** E2 refuses to
    grade a kill that interrupted nothing (`at_kill == RUNNING`); E3 refuses to grade a bounce
    whose postmaster start time did not move. E1 asked only when the 409s *stopped*, so a lane
    where the first turn had already finished answers the very first probe with 200, records
    "accepted after 0.0s", and passes the CHAOS-1 regression having held no claim to release.

    Five seconds, not "half the lease": the claim is that the release is *explicit*, and an
    explicit release is an order of magnitude away from a lease expiry, not a factor of two. A
    threshold at lease/2 would have passed a release that never happened on a lane whose lease was
    configured short.
    """
    return bool(codes) and codes[0] == 409 and codes[-1] == 200 and waited < 5.0


async def _chaos_client_disconnect() -> Finding:
    """E1 · a client that walks away mid-turn must free the session at once, not after the lease.

    The CHAOS-1 regression, made permanent. The 2026-07 load test measured 63 seconds before a
    disconnected session accepted a new turn — the full `service_turn_claim_lease_seconds` — because
    nothing released the claim when the generator was closed. `api/routes/turns.py` now releases
    both the in-process slot and the durable claim in the stream's `finally`, which runs on client
    disconnect; that is the claim, and this is the measurement of it.

    Measured as time-to-accept rather than as an inspection of the lock, because a lock that is
    released and a session that answers are different statements and only the second one is what a
    chemist experiences.
    """
    async with httpx.AsyncClient(base_url=FRONT_DOOR, timeout=60.0) as client:
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
            # `stream`, not `post`. A successful re-POST returns an SSE body that only ends when
            # the *whole next turn* does, so reading it measured 25.5 s of turn on the first
            # attempt at this and reported it as 25.5 s of lock. The question is when the session
            # stops answering 409, which is knowable from the status line alone.
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
        # Five seconds, not "half the lease": the claim is that the release is *explicit*, and an
        # explicit release is an order of magnitude away from a lease expiry, not a factor of two.
        # A threshold at lease/2 would have passed a release that never happened on a lane whose
        # lease was configured short.
        ok=_release_was_observed(codes, waited=waited),
        observed=(
            f"accepted after {waited:.1f}s (lease is {lease}s); status codes {codes[:4]}"
            + ("" if codes[:1] == [409] else " — NEVER LOCKED, so no release was tested")
        ),
        detail="CHAOS-1 regression: this was 63 s before the claim was released on disconnect",
    )


async def _chaos_worker_killed_mid_job() -> Finding:
    """E2 · SIGKILL the connector worker mid-job; Temporal must still finish the job.

    The whole reason durability lives in Temporal rather than in layer 1 (`CLAUDE.md`), and
    until now asserted rather than shown: the thirteen Temporal test modules run against the
    time-skipping test server, where no worker is ever killed. `make live-jobs` freezes a worker
    with SIGSTOP and resumes it, which tests a *stall*; this kills the process outright and starts
    a new one, which is what a pod eviction does.

    The workflow's state at the moment of the kill is reported, not assumed. A job that had already
    completed would make this check pass having interrupted nothing — the same vacuous shape as an
    audit chain that verifies over zero rows.

    **The recovery latency is the deliverable, not the pass.** A SIGKILLed worker leaves its
    activity in `Started` against a worker identity that no longer exists, and Temporal has no
    other liveness signal for it — so the job resumes only when `xtb_job_heartbeat_timeout_seconds`
    expires. Measured here at **600 s**, exactly the configured value, with the activity pinned at
    `species 1/5` the whole time. That is the documented design (`core/config/calculators.py`: "the
    heartbeat below — not this timeout — is what detects a dead worker"), and it had never been
    measured, so the wait budget below is derived from the setting rather than guessed: a check
    that timed out at 240 s reported a durability failure that was a ten-minute detection window.
    """
    connector, job = find_job("compute_reaction_energy")
    tool = build_job_tool(connector, job)
    params_type = tool.__annotations__["params"]
    workflow_id = job_workflow_id(connector, "compute_reaction_energy", _CHAOS_PAYLOAD)

    launch = asyncio.create_task(
        tool(params_type(**_CHAOS_PAYLOAD), "storm chaos: SIGKILL the connector worker mid-job")
    )
    # Poll for RUNNING and kill the instant it is, rather than sleeping a guessed interval. A fixed
    # sleep has to be long enough for the workflow to start and short enough that the job has not
    # finished, and nothing guarantees those windows overlap — on this job at `quick` level they
    # did not. Polling removes the guess from the front half; `standard` level removes it from the
    # back half; and `at_kill` below still records what was actually true, because a check that
    # cannot detect its own vacuous pass is the thing this harness keeps having to fix.
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


async def _postmaster_start_time() -> str:
    """When the server behind `postgres_dsn` last started, as the server itself reports it.

    The identity of a *running postmaster*, which is the only thing that can distinguish a restart
    from a script that merely logged one. A restart gives the new server a new start time; a no-op
    leaves it byte-identical, and no count of surviving turns can tell those apart.

    Through `core.db.connection` like every other read in this file, not a bare `psycopg.connect`:
    `chemclaw.cli` does not own the Postgres stack (`tests/test_third_party_layering.py`), and it
    does not need to — this harness never enters `pooling()`, so the helper is a dedicated connect
    here, which is the property the read wants. The pool that must not answer this is the *front
    door's*, and that is in another process entirely.
    """
    async with db_connection(settings.postgres_dsn) as conn:
        row = await (await conn.execute("select pg_postmaster_start_time()")).fetchone()
    return str(row[0]) if row else ""


async def _chaos_postgres_bounce() -> Finding:
    """E3 · restart Postgres under load; the pool must reconnect rather than stay poisoned.

    Turns in flight across the bounce are *expected* to fail, and this does not assert otherwise —
    a database that goes away mid-query has taken the answer with it, and pretending it did not is
    the failure this repository keeps naming. What must hold is that the failure is bounded in
    time: a fresh turn afterwards has to work, without restarting the front door.

    **It also asserts that the bounce happened, and that half is not decoration.** Measured
    2026-08-28: `bootstrap.sh restart-postgres` had no compose branch, so on a Docker lane —
    the only lane this environment can run — it logged "postgres not running", then "postgres
    already accepting connections", then "postgres up", and restarted nothing;
    `pg_postmaster_start_time()` was byte-identical either side. This check reported **PASS** with
    "24/24 in-flight turns survived the bounce", which is exactly what a run doing nothing scores.
    The lane primitive is fixed, and a fix to a primitive is not a reason to keep trusting the
    check that could not see it break: the postmaster's own start time is read before and after,
    and a bounce that did not happen is now a failure of *this* check rather than a silent pass.
    """
    before = await _postmaster_start_time()
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
    after = await _postmaster_start_time()
    bounced = bool(before) and bool(after) and before != after

    return Finding(
        family="E",
        name="the front door recovers from a Postgres restart without being restarted itself",
        ok=bounced and recovered and waited < 45.0,
        observed=(
            f"postmaster start time {before!r} -> {after!r} "
            f"({'restarted' if bounced else 'NOT RESTARTED — this check proved nothing'}); "
            f"{survived}/{len(during)} in-flight turns survived the bounce; "
            f"a fresh turn answered {waited:.1f}s after it"
        ),
        detail=(
            "in-flight losses are expected; a pool that never reconnects is not"
            if bounced
            else "the lane's restart-postgres verb did not restart anything, so nothing was tested"
        ),
    )


async def _chaos_broker_outage() -> Finding:
    """E4 · with no broker, a durable launch must *say so* rather than hang or answer anyway.

    This is the 2026-08-02 incident's own shape, one step upstream: a task queue with no worker
    reached the model as "Error: Function failed." A broker that is not there at all is the harder
    case, because the launch cannot even be confirmed — and the outcome that must never happen is a
    turn where the failure is nowhere on the stream.

    **And that the broker really went away**, which is E3's postmaster reading one family over:
    a lane verb that logs a stop and stops nothing is a class this harness has already been caught
    by once, in the sibling check, on the same day.

    **What is deliberately not scored: whether the turn also produced prose.** It does, and the
    prose says the job was launched — but that text is the mock's script, replayed regardless of
    what the tool returned, so failing the check on it would be measuring this harness rather than
    the system. The system's obligation is to put the failure on the stream, which is what
    `_bad_call_was_reported` reads. What a model *does* with a reported failure is family F's
    question and a real model's job.
    """
    await asyncio.to_thread(_lane, "bootstrap.sh", "stop-temporal")
    try:
        stopped = not await _broker_is_reachable()
        (result,) = await storm("d-collide", turns=1, concurrency=1, timeout=120.0)
    finally:
        await asyncio.to_thread(_lane, "bootstrap.sh", "start-temporal")
        # Whatever died while the broker was gone comes back before anything else is measured.
        await asyncio.to_thread(_lane, "processes.sh", "up")

    return _broker_outage_finding(stopped=stopped, result=result)


async def _broker_is_reachable() -> bool:
    """Whether a Temporal client can be built against the configured address, right now.

    The analogue of E3's postmaster start time, and it is here for the same reason: measured
    2026-08-28, `bootstrap.sh restart-postgres` had no compose branch and restarted nothing while
    E3 reported PASS. A lane verb that silently does nothing is a class, not an incident, and the
    sibling check over `stop-temporal` had no way to see it — it would have failed *safe*, which
    is better than E3 did and still leaves the run unable to say whether it tested anything.
    """
    try:
        await temporal_connect()
    except Exception:  # any inability to reach the broker is the state this asks about
        return False
    return True


def _broker_outage_finding(*, stopped: bool, result: TurnResult) -> Finding:
    """E4's verdict: the broker was really gone, and the launch said so on the stream."""
    return Finding(
        family="E",
        name="a durable launch with no broker reaches the asker as an error, not as an answer",
        ok=stopped and _bad_call_was_reported(result),
        observed=(
            f"broker {'stopped' if stopped else 'STILL REACHABLE — this check tested nothing'}; "
            f"HTTP {result.status}, answered={result.answered}, error={result.error_code}, "
            f"tools_failed={result.tools_failed[:2]}, result[0]={_first_preview(result)!r}"
        ),
        detail=(
            result.transport_error or ""
            if stopped
            else "the lane's stop-temporal verb left the broker answering, so nothing was tested"
        ),
    )


async def family_e_chaos() -> list[Finding]:
    """E · break the stack while it is working, and measure what it does about it.

    Ordered by blast radius, least first. The broker outage is last because it is the only one that
    can leave the lane needing a restart, and a chaos family that poisons the families after it
    would report their failures as their own.
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


# The tool-surface sweep, in the three groups `cli/storm_behaviours.py` documents. Spelled here as
# literals rather than derived from a prefix, because `tests/test_live_storm.py` reads *this file*
# for every declared behaviour's name: a harness that named its scenarios by pattern would satisfy
# the catalogue and tell a reader nothing about what it drives.
#
# Group 1 runs for real. A tool whose body never executes proves nothing about its arguments, which
# is LOAD-1 — so every cheap tool on the surface is genuinely called here.
_T_DIRECT = (
    "t-chem-identity",
    "t-chem-species",
    "t-chem-degradation",
    "t-chem-batch",
    "t-safety-screen",
    "t-calc-properties",
    "t-calc-electronic",
    "t-calc-geometry",
    "t-calc-ledger",
    "t-calc-record",
    "t-molfp-search",
    "t-rxnfp-similar",
    "t-rxnfp-precedent",
    "t-bo-inline",
    "t-memory",
    "t-watches",
    "t-knowledge-read",
    "t-scratchpad",
    "t-attachments",
)

# Group 2: every reference in these is deliberately unresolvable, because a job id, a campaign id
# and an artifact ref are all minted by a run and cannot be put in a static catalogue. What is
# asserted is that the turn *survives* — a tool that dies rather than reporting "not on file" costs
# the chemist the turn (D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed).
_T_SURVIVES = ("t-unknown-reference", "t-scratchpad-edit", "t-clarify")

# Group 3: the launchers, driven on a dry-run turn so each is refused before anything starts.
_T_DRY_RUN = (
    "t-job-calc-screens",
    "t-job-calc-conformers",
    "t-job-calc-coordinates",
    "t-job-calc-association",
    "t-job-calc-bonds",
    "t-job-bo-campaign",
    "t-job-results",
    "t-job-report",
    "t-knowledge-write",
    "t-memory-synthesis",
    "t-template-species",
    "t-template-conformers",
    "t-template-safety",
    "t-template-bonds",
)


async def family_t_tool_surface() -> list[Finding]:
    """T · every tool the agent advertises, reached by a call the tool would actually accept.

    **The gap this closes is the one LOAD-1 is about, one level up.** Measured when this family
    was added: the other eight named five tools between them, over a surface of ninety-nine. So
    "the tool path is genuinely exercised" was a claim about 5% of it, and the 95% it said nothing
    about was the part a chemist mostly uses — every `chem`, `safety`, `molfp`, `rxnfp` and `bo`
    tool, the whole calibration ledger, the memory verbs and every template. Nothing had removed
    their coverage; the catalogue was written against the surface of the day and nothing
    re-derived it afterwards. `tests/test_storm_behaviour_coverage.py` is what re-derives it, so
    this family cannot go stale in that direction again.

    Three checks, because the three groups can honestly assert different things.

    The **direct** group is the strong one: every announced call must come back as a result, so a
    call that died in the parse-error branch before the tool body ran shows up as a missing result
    rather than as a passing turn. That is exactly the measurement the 2026-07 load test did not
    make. It is a count of results and not of *bodies* — a gate's refusal is a result too — so the
    finding is worded as "came back" rather than as "ran".

    The **survives** group asks less on purpose. Its references cannot exist — an artifact ref and
    a job id are minted by runs — so the finding is that the turn came back at all.

    The **dry-run** group is the weakest and says so. `agent/tool_authz.dry_run_refusal` refuses
    every side-effecting call on a dry-run turn, so these reach the gate with their arguments
    decoded and start nothing: it proves the call was well-formed enough to be refused, not that
    the tool body accepted it. That is the price of not starting every durable job and every
    template run this deployment declares in a lane that has to finish, and it buys the one thing
    nothing else here measures — IDEA-4's gate swept across the whole expensive surface at once. A
    launcher that slipped through it would start real work, so `not r.tools_failed` is not the
    assertion; the refusal appearing in every preview is.
    """
    findings: list[Finding] = []

    for behaviour in _T_DIRECT:
        expected = declared_calls(behaviour)
        (result,) = await storm(behaviour, turns=1, concurrency=1)
        findings.append(
            Finding(
                family="T",
                # "came back", not "the body ran": a gate's refusal is also a result, and this
                # count cannot tell the two apart. What it *can* say is that no call vanished,
                # which is the thing LOAD-1 hid — and it says it against the number the catalogue
                # declares rather than against `> 0`, because a panel of four tools that announced
                # one and returned one satisfied `announced > 0 and missing == 0` while three of
                # its four tools were never reached.
                name=f"{behaviour}: all {expected} declared call(s) came back",
                ok=_every_call_came_back(result, expected),
                observed=(
                    f"status={result.status} announced={result.announced}/{expected} "
                    f"returned={result.returned} failed={result.tools_failed}"
                ),
                detail="an announced call with no result is a call that died before the tool ran",
            )
        )

    for behaviour in _T_SURVIVES:
        expected = declared_calls(behaviour)
        (result,) = await storm(behaviour, turns=1, concurrency=1)
        findings.append(
            Finding(
                family="T",
                name=f"{behaviour}: all {expected} unresolvable reference(s) were tried, "
                "and the turn survived",
                # The count is asserted here too, weakly but not vacuously: a turn that dispatched
                # nothing at all passed `status == 200 and transport_error is None`, so "an
                # unresolvable reference does not kill the turn" was satisfied by a turn that
                # never looked one up. Whether each call answered or failed is deliberately not
                # scored — that is what makes this the weak group.
                ok=(
                    result.status == 200
                    and result.transport_error is None
                    and result.announced == expected
                ),
                observed=(
                    f"status={result.status} announced={result.announced}/{expected} "
                    f"returned={result.returned} failed={result.tools_failed} "
                    f"error={result.error_code}"
                ),
                detail="'not on file' is an answer; a dead turn is not",
            )
        )

    for behaviour in _T_DRY_RUN:
        expected = declared_calls(behaviour)
        (result,) = await storm(behaviour, turns=1, concurrency=1, dry_run=True)
        # Matched on the word the refusal itself opens with, so a launcher that slipped past the
        # gate and returned a job id cannot read as a pass.
        # `failure_messages`, not `result_previews`: a refusal is raised, so it arrives as a
        # `tool_failed` carrying the sentence and no `tool_result` at all. See the field's comment.
        refused = [m for m in result.failure_messages if "DRY RUN" in m]
        findings.append(
            Finding(
                family="T",
                name=f"{behaviour}: all {expected} launcher(s) refused on a dry-run turn",
                ok=(
                    result.status == 200
                    and result.announced == expected
                    and len(refused) == expected
                ),
                observed=(
                    f"status={result.status} announced={result.announced}/{expected} "
                    f"refused={len(refused)} failed={result.tools_failed}"
                ),
                detail="a call that was not refused started durable work this lane did not want",
            )
        )

    return findings


async def family_h_edges() -> list[Finding]:
    """H · data a chemist could plausibly send that nothing in the corpus resembles.

    Each case is verified where the damage would actually show. Unicode is read back out of
    Postgres rather than off the answer — an answer is the mock's own text and would round-trip
    through nothing — and the injection string is checked against the table it names, because "the
    turn answered" says nothing at all about whether a `DROP TABLE` ran.
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

    audit_before = await _scalar("select count(*) from audit_events")
    (inj,) = await storm("h-injection", turns=1, concurrency=1)
    audit_after = await _scalar("select count(*) from audit_events")
    findings.append(
        Finding(
            family="H",
            name="an injection string is searched for, and audit_events is still there",
            # `>=` was a bound the count meets by construction — row counts do not fall — so the
            # only run this could have failed is one where the query itself raised. Strictly
            # greater is the same claim plus the one the check's name makes: `find_notes` is an
            # audited tool, so the search that treated the string as data left a row behind.
            ok=inj.status == 200 and audit_after > audit_before,
            observed=(
                f"audit_events {audit_before} → {audit_after}; the search itself is the "
                f"{audit_after - audit_before} new row(s)"
            ),
            detail="the string asks for `DROP TABLE audit_events`; the row count is the answer",
        )
    )

    (smiles,) = await storm("h-bad-smiles", turns=1, concurrency=1)
    findings.append(
        Finding(
            family="H",
            name="an unparseable reaction SMILES does not kill the turn — the call came back",
            # `_completed_without_dying` passed on the behaviour's own `empty_answer`, so a turn
            # in which `gather_evidence` was never dispatched scored identically to one in which
            # it ran and returned the three retrievers that could still answer.
            ok=_every_call_came_back(smiles, declared_calls("h-bad-smiles")),
            observed=(
                f"HTTP {smiles.status}, answered={smiles.answered}, error={smiles.error_code}, "
                f"announced={smiles.announced} returned={smiles.returned}, "
                f"result[0]={_first_preview(smiles)!r}"
            ),
            # Empty rather than an error is the *documented* contract, not an oversight:
            # `FingerprintReactionRetriever.retrieve` answers only what its source can, and a
            # gather that raised because one of four retrievers could not parse an optional anchor
            # would lose the three that could. The conversational search tools are where "nothing
            # similar" has to be distinguishable from "I could not read that", and they do.
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


def _accounting_is_clean(rows: Sequence[dict[str, Any]]) -> bool:
    """No turn vanished — *and* turns were offered, which is the half a zero count cannot say.

    `lost == 0` is another assertion whose negative form is trivially satisfied: `--sweep-turns 0`
    produces rows in which nothing was offered, nothing was dropped, and the accounting passes
    over zero observations. A count of zero is evidence only once something has been counted.
    """
    return (
        bool(rows)
        and all(int(row["turns"]) > 0 for row in rows)
        and sum(int(row["unaccounted"]) for row in rows) == 0
    )


def _turn_outcomes(results: Sequence[TurnResult]) -> dict[str, int]:
    """Every turn in exactly one bucket, each counted independently rather than by subtraction.

    **This exists because the check over it could not fail.** The sweep reported `accepted` and
    then defined `failed = turns - accepted`, and the finding asserted `accepted + failed ==
    turns` — an identity that holds for any sweep whatsoever, including one where every turn was
    dropped on the floor. "Every offered turn is accounted for" names something a run *can* fail,
    so the buckets have to be arrived at separately and the leftovers have to be visible.

    `silent` is the bucket the identity hid: HTTP 200, no error code and no answer — the turn
    ended and nothing anywhere says what happened to it, which is
    `D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` measured at the front door.

    `accepted` requires `answered`, because the sweep's own docstring defines goodput as "turns
    that answered, per second" and the code counted 200-and-no-error — which is that bucket plus
    `silent`, inflating the one number SCALE-3 is read off.
    """
    outcomes = {"accepted": 0, "shed": 0, "errored": 0, "dropped": 0, "silent": 0}
    for result in results:
        if result.transport_error is not None or result.status not in (200, 429, 503):
            outcomes["dropped"] += 1
        elif result.status in (429, 503):
            outcomes["shed"] += 1
        elif result.error_code is not None:
            outcomes["errored"] += 1
        elif result.answered:
            outcomes["accepted"] += 1
        else:
            outcomes["silent"] += 1
    return outcomes


async def family_a_admission(
    *, sweep_turns: int, offered: int, repeats: int
) -> tuple[list[Finding], list[dict[str, Any]]]:
    """A · what the admission cap actually buys, swept by restarting the front door at each value.

    **This is SCALE-3, and the previous sweep was not.** It varied *offered* load against a fixed
    `service_max_concurrent_turns=8`, which measures how much load the door will shed and says
    nothing about where raising the cap stops helping — the question the backlog row has held open
    since July. The cap is read once at startup into a semaphore, so the only honest way to sweep
    it is to restart the process at each value, which is what this does.

    **Throughput here means goodput — turns that answered, per second — and the distinction is the
    difference between an answer and its opposite.** The first run of this sweep reported
    `len(results) / elapsed`, which counts a shed turn as a completed one: at cap 2 it read
    6.65 turns/s (with 6 of 48 answering) against 2.45 turns/s at cap 32 (with 32 answering), and
    concluded that raising the cap *hurt*. Draining a queue by refusing it is fast. Counting only
    the turns that answered inverts the finding: 0.83 → 1.08 → 1.43 → 1.65 → 1.63 accepted/s, which
    rises with the cap and then stops.

    **Each cap is sampled `repeats` times and the knee is judged against the spread those samples
    show, not against a threshold chosen in advance.** This is D-2026-08-04-a-plateau-needs-the-
    noise-you-measured-it-with, applied to the harness that was written the day after it: the first
    version declared a knee wherever a step bought less than a fixed 10 %, and its own docstring
    asserted that "the measurement's own run-to-run spread is a few percent, which is why the
    threshold is well outside it". Nothing had measured the spread. Three single-sample runs then
    put the 8 → 16 step at **+6.3 %, +3.9 % and +13.5 %** — straddling the threshold — so the same
    stack answered "the knee is at 8" twice and "no knee in range" once. A plateau test that
    supplies its own noise reproduces exactly the error that ADR exists to prevent, with a
    harness's authority behind it.

    So the sweep now measures what it needs. Each cap's goodput is the **median** of its samples,
    and the noise floor is the largest within-cap spread seen anywhere in the sweep — an honest
    upper bound on what one sample can be wrong by, taken from this run rather than from memory.

    Four mechanical verdicts, because a table is a measurement and not a check: **no turn may
    vanish**, the cap must be **load-bearing** (goodput at the top above goodput at the bottom —
    otherwise the setting is decoration and an operator tuning it is wasting a restart), the sweep
    must **resolve** the knee against its own noise, and the noise itself must be **small enough for
    the answer to mean anything** — which is the check that would have caught the first version.
    """
    rows: list[dict[str, Any]] = []
    try:
        for cap in _ADMISSION_CAPS:
            samples: list[float] = []
            drains: list[float] = []
            accepted = failed = turns = 0
            unaccounted = 0
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
                outcomes = _turn_outcomes(results)
                accepted = outcomes["accepted"]
                turns, failed = len(results), len(results) - accepted
                unaccounted += outcomes["dropped"] + outcomes["silent"]
                p50, p95 = percentiles(results)
                samples.append(accepted / max(elapsed, 0.001))
                drains.append(turns / max(elapsed, 0.001))
                # The restart is inside the loop on purpose: a repeat that reused the warm process
                # would measure the same process twice and report its agreement with itself as
                # reproducibility. Restarting is the thing being varied everywhere else in this
                # sweep, so it has to be varied here too.
            rows.append(
                {
                    "cap": cap,
                    "offered": offered,
                    "turns": turns,
                    "accepted": accepted,
                    "failed": failed,
                    # Turns that neither answered nor said why. Zero on a healthy sweep, and the
                    # only number in this row a check can actually fail on.
                    "unaccounted": unaccounted,
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

    lost = sum(int(row["unaccounted"]) for row in rows)
    findings = [
        Finding(
            family="A",
            name="every offered turn ended with an answer or a stated reason",
            ok=_accounting_is_clean(rows),
            observed=(
                f"{len(rows)} cap(s) swept, {lost} turn(s) that neither answered nor "
                f"reported why (dropped or silently empty)"
            ),
            detail=(
                "this used to compare `accepted + failed` against `turns` with `failed` defined "
                "as `turns - accepted`, which is an identity and cannot fail"
            ),
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
            # The sample count is part of this verdict, not just part of its prose. At
            # `--sweep-repeats 1` every cap measures a spread of exactly zero, so this check
            # passed with nothing measured and `_knee` then fired on the first pair that failed to
            # improve at all — the fabricated knee, reached through the door the ceiling does not
            # cover.
            ok=_sweep_is_readable(rows) and noise(rows) <= _MAX_READABLE_NOISE,
            observed=(
                f"largest within-cap spread {noise(rows) * 100:.0f}% "
                f"over {_samples_per_cap(rows)} sample(s) per cap "
                f"(ceiling {_MAX_READABLE_NOISE * 100:.0f}%, "
                f"minimum {_MIN_SAMPLES_PER_CAP} sample(s))"
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
            # `_knee` returns None for two unrelated reasons and this row used to assert the
            # first of them either way, so a sweep too noisy to see anything reported the system
            # as still improving at the top of the range.
            observed=_knee_observation(rows),
            detail="SCALE-3's actual question: how high is worth setting this",
        ),
    ]
    return findings, rows


# The most within-cap disagreement a sweep may show and still be read for a knee. Above it, one
# sample's error is bigger than the steps being compared, so any knee is a coin flip — the check
# above says so rather than letting the knee check report an artefact as an answer.
_MAX_READABLE_NOISE = 0.15

# The fewest samples per cap a spread may be computed from and still be read as a noise figure.
# One sample measures a spread of exactly zero, which is not a small noise floor — it is no
# measurement at all, and it disarms both the ceiling above and the comparison inside `_knee`.
_MIN_SAMPLES_PER_CAP = 2


def _sweep_is_readable(rows: Sequence[dict[str, Any]]) -> bool:
    """The sweep measured enough, at more than one sample, for a spread to mean anything.

    Two ways a noise figure can be a number and not a measurement, and the check over it passed
    both. One sample per cap measures a spread of exactly zero by construction. And `spread`
    divides by `max(median, 1e-9)`, so a cap where *nothing answered* also reports 0 % rather than
    an undefined value — which made "the sweep's own noise is small enough to read a knee against"
    true of a sweep with nothing in it to read.
    """
    return (
        bool(rows)
        and _samples_per_cap(rows) >= _MIN_SAMPLES_PER_CAP
        and all(float(row["goodput"]) > 0 for row in rows)
    )


def _samples_per_cap(rows: Sequence[dict[str, Any]]) -> int:
    """The fewest samples any cap contributed — the weakest link in this sweep's noise figure."""
    return min((len(row["samples"]) for row in rows), default=0)


def noise(rows: Sequence[dict[str, Any]]) -> float:
    """The largest within-cap spread anywhere in the sweep, as a fraction of that cap's median.

    An upper bound on how wrong one sample can be, taken from *this* run. That is the whole point:
    a plateau test that supplies its own noise figure reproduces the error
    D-2026-08-04-a-plateau-needs-the-noise-you-measured-it-with exists to prevent, and this harness
    reproduced it — a fixed 10 % threshold, and a docstring asserting the spread was "a few
    percent" with nothing having measured it.
    """
    return max((float(row["spread"]) for row in rows), default=0.0)


def _knee(rows: Sequence[dict[str, Any]]) -> int | None:
    """The first cap whose successor buys less improvement than the sweep's own noise floor.

    Judged against `noise(rows)` rather than a constant, because a step smaller than the spread
    between two runs of the *same* cap is not a step anyone measured. Three single-sample runs of
    this sweep put the 8 → 16 step at +6.3 %, +3.9 % and +13.5 %; against a fixed 10 % the same
    stack answered "the knee is at 8" twice and "no knee in range" once.

    **None also when the noise itself is too large to read a knee against, and that guard is not
    optional.** A noise floor makes this function fire *sooner*, not later — "the step is smaller
    than the spread" is a statement about the measurement, and at a large enough spread every step
    satisfies it and the knee lands on the first pair. So without the ceiling, a sweep too noisy to
    say anything would confidently report the smallest cap as the answer: the failure mode is not a
    missing knee but a fabricated one. Found by writing the test for the opposite behaviour and
    watching it fail.

    None means "we do not know yet" in both cases — the sweep ended before the system did, or it
    could not see well enough to tell. Neither is the top of the range dressed up as an answer.
    """
    floor = noise(rows)
    if not _sweep_is_readable(rows):
        return None
    if floor > _MAX_READABLE_NOISE:
        return None
    for lower, upper in zip(rows, rows[1:], strict=False):
        if upper["goodput"] < lower["goodput"] * (1 + floor):
            return int(lower["cap"])
    return None


def _knee_observation(rows: Sequence[dict[str, Any]]) -> str:
    """What the knee check actually found — including *which* kind of "we do not know" it is.

    `_knee` answers `None` for three unrelated reasons: the sweep took too few samples to have a
    noise figure at all, the noise it did measure is above what a step can be read against, and
    the goodput was still climbing when the range ran out. Only the last of those is a statement
    about the system, and the finding's observed text asserted it in all three cases — so a sweep
    that could not see anything reported "the sweep's top is a limit of the sweep, not of the
    system", which is a confident claim built on a measurement that refused to answer.
    """
    if not rows:
        return "no rows"
    floor = noise(rows)
    samples = _samples_per_cap(rows)
    if not _sweep_is_readable(rows):
        return (
            f"unreadable: {samples} sample(s) per cap and goodput "
            f"{[round(float(row['goodput']), 2) for row in rows]} — a spread needs more than one "
            f"sample and a median above zero to be a measurement rather than a number. Raise "
            f"--sweep-repeats to at least {_MIN_SAMPLES_PER_CAP}, and check that turns answered"
        )
    if floor > _MAX_READABLE_NOISE:
        return (
            f"unreadable: the largest within-cap spread is {floor * 100:.0f}%, above the "
            f"{_MAX_READABLE_NOISE * 100:.0f}% ceiling — one sample's error is wider than the "
            f"steps being compared, so no knee is claimed. Raise --sweep-repeats"
        )
    knee = _knee(rows)
    if knee is None:
        return (
            f"no cap in {_ADMISSION_CAPS} stops paying by more than the {floor * 100:.0f}% noise "
            "floor — the sweep's top is a limit of the sweep, not of the system"
        )
    return (
        f"goodput stops improving at cap {knee} "
        f"(steps must beat the {floor * 100:.0f}% noise floor over {samples} samples)"
    )


def report(
    findings: Sequence[Finding],
    sweep: Sequence[dict[str, Any]],
    notes: dict[str, Any],
    planned: Sequence[str],
) -> str:
    """The run as tables — every row an observation, none of them a paraphrase.

    The coverage table comes first and is the reason this signature grew a `planned` argument. A
    pass count answers "did what ran, run clean"; only the difference between planned and observed
    families answers "did it run". Reporting the first while the reader infers the second is how
    this harness printed 17/17 for a matrix two families short of what it documented.
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
    lines.append("| family | what it covers | checks |")
    lines.append("| --- | --- | ---: |")
    for letter in planned:
        count = sum(1 for finding in findings if finding.family == letter)
        lines.append(f"| {letter} | {FAMILIES.get(letter, '?')} | {count or '**0**'} |")
    lines.append("")

    if sweep:
        lines.append("## A · admission cap swept (SCALE-3)\n")
        lines.append(
            f"Offered load held at {sweep[0]['offered']} concurrent, "
            f"{sweep[0]['turns']} turns per step; the front door restarted at each cap.\n"
        )
        lines.append(
            "| cap | accepted | shed/error | p50 s | p95 s | answered/s | offered drained/s |"
        )
        lines.append("| ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for row in sweep:
            lines.append(
                f"| {row['cap']} | {row['accepted']} | {row['failed']} | "
                f"{row['p50']:.1f} | {row['p95']:.1f} | {row['goodput']:.2f} | {row['drain']:.2f} |"
            )
        lines.append(
            "\nThe last column is not throughput — it counts a shed turn as a drained one, so "
            "refusing fast reads as going fast. `answered/s` is the measurement."
        )
        lines.append("")

    lines.append("## Findings\n")
    lines.append("| family | check | result | observed |")
    lines.append("| --- | --- | --- | --- |")
    for finding in findings:
        verdict = "PASS" if finding.ok else "**FAIL**"
        lines.append(f"| {finding.family} | {finding.name} | {verdict} | {finding.observed} |")
    passed = sum(1 for f in findings if f.ok)
    lines.append(f"\n**{passed}/{len(findings)} checks passed**, over the families that ran.")
    return "\n".join(lines) + "\n"


def _mock_is_serving_the_front_door(before: int, after: int) -> bool:
    """The mock's counter moved because *this process* asked the front door for a turn.

    **A mock that is merely listening proves nothing.** The precondition below used to be a GET of
    the stats endpoint, and a mock left running by an earlier lane answers that while
    `CHEMCLAW_LLM_BASE_URL` points at a real endpoint — so the storm would drive hundreds of paid
    turns having recorded that it could not. The two facts differ by exactly one probe turn.

    `mock_requests` reports `-1` when it could not read the endpoint at all, so a negative reading
    on either side is not a movement.
    """
    return before >= 0 and after > before


def _mock_reconciliation(*, served: int, turns: int) -> Finding:
    """The zero-live-model claim as a check, against the turns this process actually drove.

    `MockLlm`'s own docstring says the counter exists because "no LLM calls were made" is a claim
    the storm has to be able to prove, "and reconciling this number against the turn count is
    how". Nothing reconciled it: the number sat in the report's notes beside `ANTHROPIC_API_KEY
    set: False`, which proves only that one vendor was not reached.

    At least one model call per turn, so `served >= turns` is the floor. It is a floor rather than
    an equality because a turn makes a second call to read its tool results back, an injected HTTP
    500 is retried `llm_max_retries` times, and two families open their own client and are not
    counted — every one of those only makes the true ratio larger.

    A run that drove no turns has proved nothing, whatever the counter reads.
    """
    return Finding(
        family="M",
        name="every model call this run made was served by the mock",
        ok=turns > 0 and served >= turns,
        observed=f"{served} mock request(s) served against {turns} turn(s) driven",
        detail=(
            "at least one model call per turn; fewer means some turn's call went somewhere this "
            "harness cannot see"
        ),
    )


async def _require_mock_lane() -> None:
    """Fail before any work if the lane is not pointed at the mock model.

    Every family here assumes the mock: the shapes family asks for malformed tool calls by name,
    and family D restarts `mock-llm` mid-flight. Against a real-model lane the first of those to
    touch the process died on `no .live/run/mock-llm.pid — is the lane up?`, which is both alarming
    and wrong — the lane *is* up, it is simply serving a real model, and by then the storm had
    already driven a few hundred turns through it.

    `processes.sh` starts `mock-llm` only when `CHEMCLAW_LLM_BASE_URL` names it, so its stats
    endpoint answering is the honest precondition: it is the same fact, asked of the thing itself
    rather than of a pid file.

    Raises:
        RuntimeError: The mock is not serving, with the setting that would fix it.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(MOCK_STATS)
        reachable = response.status_code == 200
    except httpx.HTTPError:
        reachable = False

    if not reachable:
        raise RuntimeError(
            f"the mock model is not answering at {MOCK_STATS}, so this storm would drive a real "
            "model at load — cost, rate limits, and none of the malformed shapes it exists to "
            "test. Bring the lane up pointed at the mock: "
            "CHEMCLAW_LLM_BASE_URL=http://127.0.0.1:8820/v1 make live-up. "
            "Note that `make live-e2e-full-stack` deliberately runs a real model and is a "
            "different lane from this one."
        )

    # Reachable is not the same fact as *used*. One probe turn, and the counter has to move.
    before = await mock_requests()
    await storm("a-cheap", turns=1, concurrency=1)
    after = await mock_requests()
    if not _mock_is_serving_the_front_door(before, after):
        raise RuntimeError(
            f"a mock is listening at {MOCK_STATS} but the front door is not using it: one probe "
            f"turn moved its counter {before} -> {after}. Either the front door is down, or "
            "CHEMCLAW_LLM_BASE_URL names something else and this storm would drive a real model "
            "at load. Restart the lane with CHEMCLAW_LLM_BASE_URL=http://127.0.0.1:8820/v1."
        )


async def _run_family(letter: str, run: Callable[[], Awaitable[list[Finding]]]) -> list[Finding]:
    """Run one family, turning an exception into a failed finding rather than into no report.

    Only the chaos family used to catch its own, and every other one could end the process: a
    twenty-minute run that raised in family G — `created.json()["session_id"]` on a front door
    that refused the POST is one line away — lost every finding families C, D and F had already
    made, wrote no report, and told a reader nothing about what broke. The family is still
    *observed* in the coverage table either way, which is the property the exit code depends on.
    """
    try:
        return await run()
    except Exception as exc:  # a family's failure is a finding, not a crash
        logger.exception("family %s raised", letter)
        return [
            Finding(
                family=letter,
                name=f"family {letter} ran to completion",
                ok=False,
                observed=f"the family itself raised {type(exc).__name__}: {exc}",
                detail="every check this family would have made is missing from this run",
            )
        ]


async def run_storm(
    *, sweep_turns: int, offered: int, collide: int, repeats: int, planned: Sequence[str]
) -> tuple[list[Finding], list[dict[str, Any]]]:
    """Run each planned family in turn; return every finding and the admission sweep's rows.

    Ordered so the destructive families come last: A restarts the front door at five different
    caps and E kills processes, so anything they disturb must already have been measured. B is
    genuinely last because it reads the audit trail every family before it wrote to.

    The mock is restarted *first*, for the two reasons `family_d_durable` records: its collision
    payload has to be colder than any earlier run's, and its request counter has to start at zero
    at a point before any measured turn, or the run's own zero-live-model proof is missing
    whatever happened before the restart.
    """
    global _turns_driven
    await _require_mock_lane()
    await asyncio.to_thread(_lane, "processes.sh", "restart", "mock-llm")
    _turns_driven = 0

    findings: list[Finding] = []
    sweep: list[dict[str, Any]] = []
    selected = set(planned)

    if "C" in selected:
        findings.extend(await _run_family("C", family_c_shapes))
    if "D" in selected:
        findings.extend(await _run_family("D", lambda: family_d_durable(collide)))
    if "F" in selected:
        findings.extend(await _run_family("F", family_f_adversarial))
    if "G" in selected:
        findings.extend(await _run_family("G", family_g_limits))
    if "H" in selected:
        findings.extend(await _run_family("H", family_h_edges))
    if "T" in selected:
        findings.extend(await _run_family("T", family_t_tool_surface))
    if "A" in selected:
        try:
            admission, sweep = await family_a_admission(
                sweep_turns=sweep_turns, offered=offered, repeats=repeats
            )
            findings.extend(admission)
        except Exception as exc:  # same rule as `_run_family`, minus the rows
            logger.exception("family A raised")
            findings.append(
                Finding(
                    family="A",
                    name="family A ran to completion",
                    ok=False,
                    observed=f"the family itself raised {type(exc).__name__}: {exc}",
                )
            )
    if "E" in selected:
        findings.extend(await _run_family("E", family_e_chaos))
    if "B" in selected:
        findings.extend(
            await _run_family(
                "B", lambda: family_b_tool_truth(["find_notes", "gather_evidence", "expand_note"])
            )
        )
    # Always, whatever `--families` selected: the claim is about the run, not about a scenario.
    findings.append(_mock_reconciliation(served=await mock_requests(), turns=turns_driven()))
    return findings, sweep


def main(argv: list[str] | None = None) -> int:
    """Run the storm and write its report; exit non-zero if a check failed *or* a family did not.

    A planned family that produced nothing is an error, not a gap in the prose. That is the whole
    correction this version carries: the exit code now depends on coverage as well as on results,
    so a matrix that quietly stops running half of itself cannot go green.
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
    # The lane check is not selectable: it is what makes every other family's numbers mean
    # anything, so a `--families C` run is still asked to prove no real model served it.
    if "M" not in planned:
        planned.append("M")

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
    ran = {finding.family for finding in findings}
    notes = {
        "families planned / ran": f"{len(planned)} / {len(ran & set(planned))}",
        # The count is still printed, and it is no longer the claim: family M is.
        "turns driven / mock requests served": f"{turns_driven()} / {asyncio.run(mock_requests())}",
        "ANTHROPIC_API_KEY set": bool(os.environ.get("ANTHROPIC_API_KEY")),
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
