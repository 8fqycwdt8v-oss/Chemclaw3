"""`python -m chemclaw.cli.live_jobs` — run a real durable job against a real Temporal and Postgres.

This is the half of the live lane that has never existed. `chemclaw.evals.live` drives the front
door with a real model, and its probe corpus says in its own headers that *"Temporal is NOT running
in this test run"* (`data/evals/probes/optimization.yaml`, `reporting.yaml`); the thirteen
Temporal test modules run against the time-skipping test server with no model, no front door and no
database. So the path a durable capability actually takes in production — agent tool →
`ConnectorJobWorkflow` on `background-jobs` → the bundle's workflow on `connector-<name>` → the
calculation cache → `job_records` → the audit chain — has been exercised only in pieces.

**Why no model is involved here.** The obvious design is to ask the agent to run a job and grade
the answer. That conflates two failures: a broker that did not run the job, and a model that did
not ask it to. Splitting them means a red result here names the durable spine and nothing else,
and it also makes the durable half runnable where no model credential exists — which is most CI
runners, and this repository's own agent containers. `make live-probes` is the other half and does
involve the model; it is a strictly later question.

**What it drives.** The real tool built by `connectors.jobs.build_job_tool` for a declared job —
not a hand-rolled `start_workflow`. That matters: the pre-flight (`prepare_job_launch`), the
idempotency key (`job_workflow_id`), the actor rule (`require_actor`) and the rationale requirement
are the *product's*, so a change to any of them changes what this checks, and a smoke test that
reimplemented the launch would keep passing while the real launcher broke.

**What it asserts.** Only things the live system can be asked for: the workflow's terminal state
from Temporal, and rows from Postgres. Nothing is scored from prose. That is the correction
D-2026-08-03 made to the fabrication metric — a signal that reads a summary is measuring the
summary — applied here from the start.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from temporalio.client import WorkflowExecutionStatus

from chemclaw.connectors.jobs import build_job_tool, job_workflow_id
from chemclaw.connectors.registry import find_job
from chemclaw.core.config import settings
from chemclaw.core.db import connection as db_connection
from chemclaw.core.temporal_client import connect as temporal_connect

logger = logging.getLogger(__name__)

# The states a workflow never leaves. Asked for rather than assumed so a wait ends on the truth
# it found instead of on the truth it wanted.
_TERMINAL = {
    WorkflowExecutionStatus.COMPLETED,
    WorkflowExecutionStatus.FAILED,
    WorkflowExecutionStatus.CANCELED,
    WorkflowExecutionStatus.TERMINATED,
    WorkflowExecutionStatus.TIMED_OUT,
}

# The job the smoke runs. `compute_reaction_energy` is chosen for three reasons and none of them
# is convenience: it is a *real* durable job (the same `ConnectorJobWorkflow` wrapper every job
# uses), its engine is `tblite` in-process so it needs no HPC and no external binary, and it writes
# to the calculation cache — which is what makes the never-recompute guarantee (D-011) observable
# rather than asserted. A QM job would need a cluster; a BO campaign would need rounds of
# observations before it wrote anything.
SMOKE_JOB = "compute_reaction_energy"

# The temperature this run's reactions are evaluated at — chosen once per process, from the clock.
#
# It looks like a decoration and it is the opposite. The workflow id is a hash of the payload
# (`job_workflow_id`), and a duplicate launch deliberately rejoins the existing run rather than
# recomputing (D-011). With a payload fixed across runs, the *second* `make live-jobs` against the
# same database would start nothing, compute nothing, and pass every check against the first run's
# residue — a lane that goes green while exercising none of the system it claims to test. That is
# precisely the failure this whole lane exists to remove, so it must not be built into it.
#
# Varying a real physical input rather than adding a nonce keeps the payload something a chemist
# could have asked for: any temperature in this range is a legitimate question, and the answer
# changes with it. Constant within the process, so the idempotency check below still derives the
# same id when it relaunches.
#
# **The modulus is the whole guarantee, and this copy had the wrong one.** `% 25` on a one-second
# grid yields exactly 25 distinct temperatures that ever exist, so after ~25 runs against one
# database the calculation cache holds all of them and every subsequent `make live-jobs` rejoins a
# completed run — the lane goes permanently green while computing nothing, which is the precise
# failure the paragraph above says it exists to remove. `cli/storm_behaviours.py` already carried
# the reasoned value after a soak measured the smaller period failing (6 of 81 rounds), and the fix
# landed in one of the three copies. 100,000 values on a 10-µK grid puts the recurrence at ~27.8
# hours, past any soak this harness runs, and every value is still a temperature a chemist could
# ask about. `tests/test_run_jitter.py` pins all three periods so a fourth copy cannot regress it.
#
# **The base is 301.15 K and not 298.15 K, and that is the second half of the same guarantee.**
# Copying the reasoned modulus here left this module and `cli/storm_behaviours.py` carrying the
# identical expression over otherwise byte-identical payloads — measured at `t = 1700000123`, both
# derived 298.15123 and the two payloads compared equal. Before that they had one value in common;
# after it they had all of them, so a `make live-jobs` launched during a soak round hashed to the
# storm's workflow id, rejoined its completed run and wrote no `job_records` row: the same "0
# job_records row(s) written" false failure, now reachable *between* harnesses. Each grid spans
# base + [0, 1) K, so the three bases (298.15, 300.0 in `live_storm`, 301.15 here) have to stay at
# least 1 K apart; `tests/test_run_jitter.py` asserts the union is disjoint rather than trusting
# that. 301.15 K is 28 °C — still a temperature a chemist could have asked for.
_RUN_TEMPERATURE_K = 301.15 + (int(time.time()) % 100_000) / 100_000.0

# Ammonia synthesis at the quick level: three species, small, and its symmetry numbers are the
# textbook ones — so a wrong answer is recognisable as wrong.
SMOKE_PAYLOAD: dict[str, Any] = {
    "kind": "reaction",
    "reactants": ["N#N", "[H][H]", "[H][H]", "[H][H]"],
    "products": ["N", "N"],
    "level": "quick",
    "temperature_k": _RUN_TEMPERATURE_K,
    "symmetry_numbers": {"N#N": 2, "[H][H]": 2, "N": 3},
}

# A second, different reaction for the wedged-worker check, so its launch cannot be answered from
# the cache the smoke has just filled: methanol hydrogenolysis, CH3OH + H2 → CH4 + H2O.
#
# It carries **its own** symmetry numbers rather than inheriting the smoke's. Reusing them was the
# first version of this check, and the job rejected it correctly — `_checked_symmetry_numbers`
# refuses a map naming species the equation does not contain — so the check failed on its own bad
# input while reading as a system fault. The lane caught it exactly as it would catch a real one,
# which is the argument for the lane; it is not an argument for leaving the payload wrong.
WEDGE_PAYLOAD: dict[str, Any] = {
    "kind": "reaction",
    "reactants": ["CO", "[H][H]"],
    "products": ["C", "O"],
    "level": "quick",
    "temperature_k": _RUN_TEMPERATURE_K,
    "symmetry_numbers": {"CO": 1, "[H][H]": 2, "C": 12, "O": 2},
}

SMOKE_RATIONALE = (
    "live-lane durable smoke: prove the connector-job path reaches Temporal, the connector "
    "worker, the calculation cache and the job record"
)


@dataclass
class Check:
    """One assertion about the live system, and what was actually observed.

    `observed` is kept even when the check passes. A green run that cannot say *what* it saw is a
    green run nobody can audit later, and the whole point of this lane is to leave evidence on
    disk rather than a claim in a terminal.
    """

    name: str
    passed: bool
    observed: str
    detail: str = ""


@dataclass
class SmokeRun:
    """Everything one smoke produced: the workflow it launched and every check over it."""

    workflow_id: str = ""
    checks: list[Check] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        """True when every check passed — the process exit code follows this and nothing else."""
        return all(check.passed for check in self.checks)


async def _launch(rationale: str) -> tuple[str, Any]:
    """Launch the smoke job through its real agent tool; return the workflow id and the result.

    The tool is built from the manifest exactly as `connectors.registry.job_tools` builds it for
    the agent, so this exercises the generated launcher rather than a copy of it.
    """
    connector, job = find_job(SMOKE_JOB)
    tool = build_job_tool(connector, job)
    params_type = tool.__annotations__["params"]
    workflow_id = job_workflow_id(connector, SMOKE_JOB, SMOKE_PAYLOAD)
    result = await tool(params_type(**SMOKE_PAYLOAD), rationale)
    return workflow_id, result


async def _workflow_status(workflow_id: str) -> WorkflowExecutionStatus | None:
    """The broker's own view of a workflow's state — the only authority on whether it ran."""
    client = await temporal_connect()
    description = await client.get_workflow_handle(workflow_id).describe()
    return description.status


async def _scalar(sql: str, params: tuple[Any, ...] = ()) -> Any:
    """One value from the live database, using the application's own connection helper."""
    async with db_connection(settings.postgres_dsn) as conn:
        cursor = await conn.execute(sql, params)
        row = await cursor.fetchone()
        return None if row is None else row[0]


async def check_workflow_completed(run: SmokeRun) -> Check:
    """The wrapper workflow reached COMPLETED, as Temporal reports it.

    The start time is reported alongside the status, so the record dates itself: a reader can see
    the execution belongs to this run rather than to some earlier one it rejoined.
    """
    client = await temporal_connect()
    description = await client.get_workflow_handle(run.workflow_id).describe()
    started = description.start_time.isoformat(timespec="seconds")
    status = description.status
    return Check(
        name="workflow reached COMPLETED",
        passed=status == WorkflowExecutionStatus.COMPLETED,
        observed=f"{status.name if status else 'not found'}, started {started}",
        detail=run.workflow_id,
    )


async def check_result_cached() -> Check:
    """The calculation landed in the Postgres cache.

    This is the D-011 guarantee made observable. The job's own result envelope travels back through
    Temporal, but the *cache* row is what makes a second ask free — and a workflow that returned a
    number without persisting it would look identical from the summary alone.
    """
    count = await _scalar(
        "select count(*) from calculation_results where calc_type like %s", ("xtb%",)
    )
    return Check(
        name="calculation cached in Postgres",
        passed=bool(count),
        observed=f"{count} xtb* row(s) in calculation_results",
    )


async def check_job_recorded(run: SmokeRun) -> Check:
    """A `job_records` row carries the run's rationale and actor (D-157).

    Recorded by `record_job` on the background queue *after* the child workflow returns, so this
    also proves the wrapper's own post-processing ran rather than just the connector's workflow.
    """
    row = await _scalar(
        "select json_build_object('rationale', rationale, 'requested_by', requested_by, "
        "'connector', connector, 'job', job)::text from job_records where job_id = %s",
        (run.workflow_id,),
    )
    if row is None:
        return Check(name="job recorded in Postgres", passed=False, observed="no job_records row")
    record = json.loads(row)
    complete = bool(record["rationale"]) and bool(record["requested_by"])
    return Check(
        name="job recorded in Postgres",
        passed=complete,
        observed=f"{record['connector']}/{record['job']} by {record['requested_by']}",
        detail=record["rationale"],
    )


async def check_idempotent(run: SmokeRun) -> Check:
    """Relaunching the identical payload rejoins the same run and computes nothing new.

    Measured, not asserted: the cache row count is read before and after, and a second *compute*
    would move it. `WorkflowAlreadyStartedError` is swallowed inside the launcher (it is the
    idempotency contract succeeding), so the only honest way to tell a rejoin from a recompute is
    to count what the recompute would have written.

    The rationale is deliberately different from the first launch's. It is excluded from the
    workflow id on purpose — two people asking the same question for different stated reasons must
    still share one run — and a lane that reused the same sentence would never notice if that
    stopped being true.
    """
    before = await _scalar("select count(*) from calculation_results")
    workflow_id, _ = await _launch("live-lane idempotency probe: the same payload, a second time")
    after = await _scalar("select count(*) from calculation_results")
    same_id = workflow_id == run.workflow_id
    return Check(
        name="duplicate launch rejoins the same run",
        passed=same_id and before == after,
        observed=f"id {'matches' if same_id else 'DIFFERS'}; cache rows {before} → {after}",
    )


async def check_pending_when_worker_wedged(run_dir: Path) -> Check:
    """A job whose connector worker is not polling comes back *pending*, not hung and not crashed.

    The one failure this lane exists to catch. `connectors/jobs.py` distinguishes three outcomes —
    a result inside the turn, a bare workflow id when the job outlives `inline_wait_seconds`, and a
    `ConnectorJobError` when the launch could not be confirmed — and until now nothing exercised
    the middle one against a real broker. It is also the shape of the 2026-08-02 incident named in
    that module's own comment: a task queue with no worker registered reached the model as
    "Error: Function failed."

    SIGSTOP rather than a kill: it freezes the worker mid-poll without unregistering it or needing
    a restart, which is both closer to a wedged process than a clean exit is and reversible in one
    signal. The payload differs from the smoke's so the launch cannot be answered from cache.
    """
    pidfile = run_dir / "worker-calc.pid"
    if not pidfile.is_file():
        return Check(
            name="wedged worker yields a pending job",
            passed=False,
            observed=f"no {pidfile} — run infra/live/processes.sh up first",
        )
    pid = int(pidfile.read_text().strip())
    connector, job = find_job(SMOKE_JOB)
    tool = build_job_tool(connector, job)
    params_type = tool.__annotations__["params"]
    expected_id = job_workflow_id(connector, SMOKE_JOB, WEDGE_PAYLOAD)

    os.kill(pid, signal.SIGSTOP)
    try:
        started = time.monotonic()
        returned = await tool(params_type(**WEDGE_PAYLOAD), "live-lane wedged-worker probe")
        waited = time.monotonic() - started
    finally:
        os.kill(pid, signal.SIGCONT)

    pending = isinstance(returned, str) and returned == expected_id
    if not pending:
        return Check(
            name="wedged worker yields a pending job",
            passed=False,
            observed=(
                f"expected the workflow id after ~{job.inline_wait_seconds}s, "
                f"got {type(returned).__name__}"
            ),
        )
    # And it really is only pending: once the worker is polling again the same run finishes.
    # Any *terminal* state ends the wait, not just the one being hoped for — a run that failed
    # while this polled for COMPLETED would otherwise be reported as "never completed", which
    # sends the reader looking for a hang instead of reading the failure Temporal already has.
    for _ in range(60):
        status = await _workflow_status(expected_id)
        if status in _TERMINAL:
            return Check(
                name="wedged worker yields a pending job",
                passed=status == WorkflowExecutionStatus.COMPLETED,
                observed=(
                    f"returned the id after {waited:.0f}s, "
                    f"then {status.name if status else 'gone'} once resumed"
                ),
            )
        await asyncio.sleep(1)
    return Check(
        name="wedged worker yields a pending job",
        passed=False,
        observed=f"returned the id after {waited:.0f}s but was still running 60s after SIGCONT",
    )


async def check_audit_chain() -> Check:
    """The GxP hash chain still verifies after the run — the same check `make audit-verify` runs.

    **The row count is reported, not just the verdict.** An empty `audit_events` verifies trivially,
    and the first live pass of this smoke passed this check against exactly that: zero rows, because
    the audit sink records *agent tool calls* and a durable job launched from a script never makes
    one. "Verifies" and "verifies over something" are different claims, and a check that cannot tell
    them apart is the same species of defect as a metric that measures its grader's blindfold
    (D-2026-08-03). The count makes a vacuous pass legible instead of reassuring.
    """
    events = await _scalar("select count(*) from audit_events")
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
        [sys.executable, "-m", "chemclaw.cli.verify_audit_chain"],
        capture_output=True,
        text=True,
        check=False,
    )
    output = (completed.stdout or completed.stderr).strip()
    verdict = output.splitlines()[-1] if output else f"exit {completed.returncode}"
    return Check(
        name="audit chain verifies",
        passed=completed.returncode == 0,
        observed=f"{verdict} (over {events} audit event(s))",
    )


async def run_smoke(run_dir: Path) -> SmokeRun:
    """Launch the job once, then ask the live system every question that has a mechanical answer."""
    run = SmokeRun()
    started = time.monotonic()
    run.workflow_id, result = await _launch(SMOKE_RATIONALE)
    run.seconds = time.monotonic() - started
    logger.info("launched %s in %.1fs", run.workflow_id, run.seconds)
    logger.debug("result: %s", result)

    checks: list[Callable[[], Awaitable[Check]]] = [
        lambda: check_workflow_completed(run),
        check_result_cached,
        lambda: check_job_recorded(run),
        lambda: check_idempotent(run),
        lambda: check_pending_when_worker_wedged(run_dir),
        check_audit_chain,
    ]
    for check in checks:
        run.checks.append(await check())
    return run


def report(run: SmokeRun) -> str:
    """The run as a table, in the same shape `cli/live_probes.py` reports its own."""
    lines = [
        "# Live durable-job smoke\n",
        f"Job `{SMOKE_JOB}` · workflow `{run.workflow_id}` · launched in {run.seconds:.1f}s",
        f"· Temporal `{settings.temporal_address}` "
        f"· Postgres `{settings.postgres_dsn.rsplit('@', 1)[-1]}`\n",
        "| check | result | observed |",
        "| --- | --- | --- |",
    ]
    for check in run.checks:
        verdict = "PASS" if check.passed else "**FAIL**"
        lines.append(f"| {check.name} | {verdict} | {check.observed} |")
    passed = sum(1 for check in run.checks if check.passed)
    lines.append(f"\n**{passed}/{len(run.checks)} checks passed.**")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the smoke and write its report; exit non-zero if any check failed."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=Path(".live/run"),
        help="where infra/live/processes.sh keeps its pid files (the wedged-worker check reads it)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="where to write the markdown report (default: alongside the live transcripts)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run = asyncio.run(run_smoke(args.run_dir))
    text = report(run)
    print(text)

    destination = args.report or Path(settings.live_probe_transcript_dir) / "durable-smoke.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text + "\n", encoding="utf-8")
    print(f"\nwritten to {destination}")
    return 0 if run.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
