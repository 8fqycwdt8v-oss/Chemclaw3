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
import json
import logging
import os
import shutil
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from chemclaw.core.config import settings
from chemclaw.core.db import connect as db_connect

logger = logging.getLogger(__name__)

# Where the front door and the mock live during a storm. Dev-only addresses, module constants for
# the same reason `connectors_dev` keeps its port here: no deployment reads them.
FRONT_DOOR = "http://127.0.0.1:8000"
MOCK_STATS = "http://127.0.0.1:8820/__mock/stats"


@dataclass
class TurnResult:
    """One turn, as the event stream reported it. The unit every family is counted in."""

    behaviour: str
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
    tools_called: list[str] = field(default_factory=list)
    tools_failed: list[str] = field(default_factory=list)
    # What each tool actually returned, as the stream previewed it. The adversarial family
    # needs this: a malformed call that comes back as a *result* is only acceptable if the
    # result says it failed, and 'a tool_result arrived' cannot tell those apart.
    result_previews: list[str] = field(default_factory=list)
    jobs_started: list[str] = field(default_factory=list)
    error_code: str | None = None
    transport_error: str | None = None
    event_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class Finding:
    """One mechanical observation the storm makes, and whether it is what should have happened."""

    family: str
    name: str
    ok: bool
    observed: str
    detail: str = ""


async def run_turn(client: httpx.AsyncClient, behaviour: str, message: str) -> TurnResult:
    """Ask the front door one turn and fold its SSE stream into a result.

    A transport failure is recorded rather than raised, the discipline `evals/live.run_probe`
    established: a storm of thousands of turns must not lose all of them because one connection
    dropped, and "the front door stopped answering" is itself the finding.
    """
    result = TurnResult(behaviour=behaviour)
    started = time.monotonic()
    try:
        created = await client.post("/sessions", json={})
        created.raise_for_status()
        result.session_id = str(created.json()["session_id"])

        async with client.stream(
            "POST", f"/sessions/{result.session_id}/messages", json={"message": message}
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
                result.event_counts[kind] = result.event_counts.get(kind, 0) + 1
                if kind == "tool_call":
                    result.tools_called.append(str(event.get("tool", "")))
                    result.announced += 1
                elif kind == "tool_result":
                    result.returned += 1
                    result.result_previews.append(str(event.get("preview", "")))
                elif kind == "tool_failed":
                    result.tools_failed.append(str(event.get("tool", "")))
                elif kind == "job_started":
                    result.jobs_started.append(str(event.get("job_id", "")))
                elif kind == "answer":
                    result.answered = bool(str(event.get("text", "")).strip())
                elif kind == "error":
                    result.error_code = str(event.get("code", "unknown"))
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        result.transport_error = f"{type(exc).__name__}: {exc}"
    result.seconds = time.monotonic() - started
    return result


async def storm(
    behaviour: str, *, turns: int, concurrency: int, timeout: float = 300.0
) -> list[TurnResult]:
    """Fire `turns` turns of one behaviour, `concurrency` of them in flight at once.

    Concurrency is the offered load, not the accepted load — the front door's admission semaphore
    (`service_max_concurrent_turns`) is the thing under test in family A, so this must be able to
    offer far more than it will accept.
    """
    semaphore = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency + 16, max_keepalive_connections=concurrency)

    async with httpx.AsyncClient(
        base_url=FRONT_DOOR, timeout=httpx.Timeout(timeout), limits=limits
    ) as client:

        async def one(index: int) -> TurnResult:
            async with semaphore:
                return await run_turn(client, behaviour, f"storm turn {index} [[{behaviour}]]")

        return list(await asyncio.gather(*(one(i) for i in range(turns))))


async def _scalar(sql: str, params: tuple[Any, ...] = ()) -> Any:
    """One value from the live database, through the application's own connection helper."""
    async with await db_connect(settings.postgres_dsn) as conn:
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
    p95 = times[min(len(times) - 1, int(len(times) * 0.95))]
    return (p50, p95)


# --------------------------------------------------------------------------- families


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

    The D-011 guarantee under contention: a duplicate launch must rejoin the existing run rather
    than recompute. Measured by what the calculation cache did, never by what a summary said —
    `k` simultaneous launches of one payload must add the rows of a single computation.
    """
    before = await _scalar("select count(*) from calculation_results")
    jobs_before = await _scalar("select count(*) from job_records")
    results = await storm("d-collide", turns=sessions, concurrency=sessions)
    after = await _scalar("select count(*) from calculation_results")
    jobs_after = await _scalar("select count(*) from job_records")

    launched = {job for r in results for job in r.jobs_started}
    ok_turns = sum(1 for r in results if r.status == 200)
    return [
        Finding(
            family="D",
            name=f"{sessions} simultaneous identical launches share one run",
            ok=len(launched) <= 1,
            observed=f"{len(launched)} distinct workflow id(s) announced across {ok_turns} turns",
        ),
        Finding(
            family="D",
            name="the collision computed at most one result set",
            ok=(after - before) <= 6,
            observed=(
                f"calculation_results {before} → {after}; job_records {jobs_before} → {jobs_after}"
            ),
            detail="a rejoin computes nothing; one cold run writes ~3-6 rows",
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
            "a truncated argument document is reported, not swallowed",
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
            # Deliberately *not* the refusal predicate. A 100 KB search string is legitimate input,
            # and the measured behaviour is that `find_notes` ran and returned `[]` — surviving it
            # is the correct outcome, so asserting a refusal was this check being wrong about what
            # good looks like rather than the system being wrong.
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
            "an upstream model outage reaches the asker as an error",
            lambda r: r.error_code is not None or r.status != 200,
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


async def family_b_tool_truth(expect_tools: Sequence[str]) -> list[Finding]:
    """B · did tool *bodies* actually run — asked of the audit trail, not of the turn.

    LOAD-1 made permanent. The previous load test reported "100 tool calls, the tool path is
    genuinely exercised" while every call had died in MAF's parse-error branch before any tool body
    ran; the truth was only recoverable afterwards from `audit_events`. So a turn count is never
    allowed to stand in for a tool count here.
    """
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


def report(
    findings: Sequence[Finding], sweep: Sequence[dict[str, Any]], notes: dict[str, Any]
) -> str:
    """The run as tables — every row an observation, none of them a paraphrase."""
    lines = ["# Storm — mock-driven stress, chaos and adversarial pass\n"]
    lines.append(f"Front door `{FRONT_DOOR}` · Temporal `{settings.temporal_address}` · ")
    lines.append(f"Postgres `{settings.postgres_dsn.rsplit('@', 1)[-1]}`\n")

    for key, value in notes.items():
        lines.append(f"- **{key}**: {value}")
    lines.append("")

    if sweep:
        lines.append("## A · admission sweep\n")
        lines.append("| offered | accepted | shed/error | p50 s | p95 s | turns/s |")
        lines.append("| ---: | ---: | ---: | ---: | ---: | ---: |")
        for row in sweep:
            lines.append(
                f"| {row['offered']} | {row['accepted']} | {row['failed']} | "
                f"{row['p50']:.1f} | {row['p95']:.1f} | {row['rate']:.2f} |"
            )
        lines.append("")

    lines.append("## Findings\n")
    lines.append("| family | check | result | observed |")
    lines.append("| --- | --- | --- | --- |")
    for finding in findings:
        verdict = "PASS" if finding.ok else "**FAIL**"
        lines.append(f"| {finding.family} | {finding.name} | {verdict} | {finding.observed} |")
    passed = sum(1 for f in findings if f.ok)
    lines.append(f"\n**{passed}/{len(findings)} checks passed.**")
    return "\n".join(lines) + "\n"


async def run_storm(
    *, sweep_turns: int, collide: int
) -> tuple[list[Finding], list[dict[str, Any]]]:
    """The matrix: the admission sweep, then every family that has a mechanical verdict."""
    findings: list[Finding] = []
    sweep: list[dict[str, Any]] = []

    # A · offered load well past the accepted load, so admission control is the thing measured.
    for offered in (4, 16, 48):
        started = time.monotonic()
        results = await storm("a-cheap", turns=sweep_turns, concurrency=offered)
        elapsed = time.monotonic() - started
        accepted = sum(1 for r in results if r.status == 200 and r.error_code is None)
        failed = len(results) - accepted
        p50, p95 = percentiles(results)
        sweep.append(
            {
                "offered": offered,
                "accepted": accepted,
                "failed": failed,
                "p50": p50,
                "p95": p95,
                "rate": len(results) / max(elapsed, 0.001),
            }
        )
        logger.info("sweep offered=%d accepted=%d p50=%.1fs", offered, accepted, p50)

    findings.extend(await family_c_shapes())
    findings.extend(await family_d_durable(collide))
    findings.extend(await family_f_adversarial())
    findings.extend(await family_g_limits())
    findings.extend(await family_b_tool_truth(["find_notes", "gather_evidence"]))
    return findings, sweep


def main(argv: list[str] | None = None) -> int:
    """Run the storm and write its report; exit non-zero if any mechanical check failed."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sweep-turns", type=int, default=48, help="turns per sweep step")
    parser.add_argument("--collide", type=int, default=12, help="simultaneous identical launches")
    parser.add_argument("--report", type=Path, default=Path("tasks/live-test/storm.md"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    started = time.monotonic()
    findings, sweep = asyncio.run(run_storm(sweep_turns=args.sweep_turns, collide=args.collide))
    served = asyncio.run(mock_requests())

    notes = {
        "mock requests served": served,
        "ANTHROPIC_API_KEY set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "wall clock": f"{time.monotonic() - started:.0f} s",
        "disk free": f"{shutil.disk_usage('.').free // 1_000_000_000} GB",
    }
    text = report(findings, sweep, notes)
    print(text)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(text, encoding="utf-8")
    print(f"written to {args.report}")
    return 0 if all(f.ok for f in findings) else 1


if __name__ == "__main__":
    raise SystemExit(main())
