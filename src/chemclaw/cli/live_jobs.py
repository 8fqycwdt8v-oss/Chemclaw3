"""`python -m chemclaw.cli.live_jobs` — run a real durable job against a real Temporal and Postgres.

Exercises the production durable path end to end: agent tool → `ConnectorJobWorkflow` on
`background-jobs` → the bundle's workflow on `connector-<name>` → the calculation cache →
`job_records`.

No model is involved, so a red result names the durable spine and nothing else, and the lane runs
without a model credential; `make live-probes` covers the model. The job is launched through the
real tool `connectors.jobs.build_job_tool` builds, so the pre-flight, idempotency key, actor rule
and rationale requirement checked are the product's. Assertions read only Temporal's terminal
state and Postgres rows, never prose.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from temporalio.client import WorkflowExecutionStatus

from chemclaw.cli.chat import resolve_identity
from chemclaw.connectors.jobs import build_job_tool, job_workflow_id
from chemclaw.connectors.registry import find_job
from chemclaw.core.config import settings
from chemclaw.core.db import _redact
from chemclaw.core.db import connection as db_connection
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.logging import configure_logging
from chemclaw.core.markdown import render_table
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

# The job the smoke runs: a real durable job through the shared `ConnectorJobWorkflow` wrapper that
# writes to the calculation cache, which makes the never-recompute guarantee (D-011) observable.
SMOKE_JOB = "compute_reaction_energy"

# The temperature this run's reactions are evaluated at — chosen once per process, from the clock.
#
# The workflow id is a hash of the payload and a duplicate launch rejoins the existing run (D-011),
# so a payload fixed across runs would let a second run against the same database compute nothing
# and pass on the first run's residue. A real physical input varies instead of a nonce, constant
# within the process so the idempotency check derives the same id.
#
# 100,000 values on a 10-µK grid give a ~27.8-hour period. The base (301.15 K) keeps this grid
# disjoint from `storm_behaviours` (298.15) and `live_storm` (300.0); each spans base + [0, 1) K.
# `tests/test_run_jitter.py` asserts both properties.
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
# the cache the smoke has just filled: methanol hydrogenolysis, CH3OH + H2 → CH4 + H2O. It carries
# its own symmetry numbers, since `_checked_symmetry_numbers` refuses a map naming other species.
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

    `observed` is kept even on a pass, so the record on disk says what was seen.
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

    Built from the manifest exactly as `connectors.registry.job_tools` builds it for the agent.
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


#: Between two asks of the broker while waiting on a workflow. A describe is one cheap RPC, and a
#: second is well under the resolution anyone reads a job's duration at.
_POLL_SECONDS = 1.0


async def _await_terminal(workflow_id: str) -> tuple[WorkflowExecutionStatus | None, float]:
    """Poll until the workflow reaches a terminal state or the wait runs out; say which and when.

    Any terminal state ends the wait, so a failed run is reported as failed rather than "never
    completed". Bounded by `live_jobs_terminal_wait_seconds`; a stuck job is reported with its
    state.

    Returns:
        The last status the broker reported, and the seconds this waited for it.
    """
    started = time.monotonic()
    deadline = started + settings.live_jobs_terminal_wait_seconds
    while True:
        status = await _workflow_status(workflow_id)
        if status in _TERMINAL or time.monotonic() >= deadline:
            return status, time.monotonic() - started
        await asyncio.sleep(_POLL_SECONDS)


async def _scalar(sql: str, params: tuple[Any, ...] = ()) -> Any:
    """One value from the live database, using the application's own connection helper."""
    async with db_connection(settings.postgres_dsn) as conn:
        cursor = await conn.execute(sql, params)
        row = await cursor.fetchone()
        return None if row is None else row[0]


async def check_workflow_completed(run: SmokeRun) -> Check:
    """The wrapper workflow reached COMPLETED, as Temporal reports it.

    The launch may return a bare workflow id when the job outlives `inline_wait_seconds`, so this
    waits for a terminal state before judging. The start time is reported so the record shows the
    execution belongs to this run.
    """
    status, waited = await _await_terminal(run.workflow_id)
    client = await temporal_connect()
    description = await client.get_workflow_handle(run.workflow_id).describe()
    started = description.start_time.isoformat(timespec="seconds")
    still = "" if status in _TERMINAL else f" after waiting {waited:.0f}s"
    return Check(
        name="workflow reached COMPLETED",
        passed=status == WorkflowExecutionStatus.COMPLETED,
        observed=f"{status.name if status else 'not found'}{still}, started {started}",
        detail=run.workflow_id,
    )


async def check_result_cached() -> Check:
    """The calculation landed in the Postgres cache.

    The D-011 guarantee made observable: a workflow that returned a number without persisting it
    would look identical from the result alone.
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
    """A `job_records` row carries the run's rationale and actor.

    Written by `record_job` after the child workflow returns, so this also proves the wrapper's
    post-processing ran.
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

    The launcher swallows `WorkflowAlreadyStartedError`, so the cache row count is read before and
    after: a recompute would move it. The rationale differs from the first launch's because it is
    excluded from the workflow id — different stated reasons must still share one run.
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

    `connectors/jobs.py` has three outcomes — a result inside the turn, a bare workflow id when the
    job
    outlives `inline_wait_seconds`, and `ConnectorJobError` when the launch is unconfirmed; this
    exercises the middle one. SIGSTOP freezes the worker mid-poll without unregistering it and is
    reversible in one signal. The payload differs from the smoke's so cache cannot answer it.
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
    status, resumed = await _await_terminal(expected_id)
    if status in _TERMINAL:
        return Check(
            name="wedged worker yields a pending job",
            passed=status == WorkflowExecutionStatus.COMPLETED,
            observed=(
                f"returned the id after {waited:.0f}s, "
                f"then {status.name if status else 'gone'} once resumed"
            ),
        )
    return Check(
        name="wedged worker yields a pending job",
        passed=False,
        observed=(
            f"returned the id after {waited:.0f}s but was still "
            f"{status.name if status else 'not found'} {resumed:.0f}s after SIGCONT"
        ),
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
    ]
    for check in checks:
        run.checks.append(await check())
    return run


def report(run: SmokeRun) -> str:
    """The run as a table, in the same shape `cli/live_probes.py` reports its own."""
    lines = [
        "# Live durable-job smoke\n",
        f"Job `{SMOKE_JOB}` · workflow `{run.workflow_id}` · launched in {run.seconds:.1f}s",
        f"· Temporal `{settings.temporal_address}` · Postgres `{_redact(settings.postgres_dsn)}`\n",
        render_table(
            ["check", "result", "observed"],
            [
                [check.name, "PASS" if check.passed else "**FAIL**", check.observed]
                for check in run.checks
            ],
        ),
    ]
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
        help="where to write the markdown report (default: a per-run dir under the transcripts)",
    )
    parser.add_argument(
        "--actor",
        default=None,
        help=f"the identity this smoke launches its job as (default: {settings.cli_admin_actor!r})",
    )
    args = parser.parse_args(argv)

    configure_logging()
    # A user-triggered job must name its actor: `prepare_job_launch` calls `require_actor()`, which
    # refuses an unauthenticated launch under `entra_required`. Resolved through
    # `chat.resolve_identity` so the smoke uses the configured `cli_admin_actor`/`cli_admin_roles`
    # rather than inventing an entitlement; with no roles, an `expensive: true` job is refused here.
    actor, roles = resolve_identity(admin=True, actor=args.actor)
    identity = set_current_identity(actor, roles)
    try:
        run = asyncio.run(run_smoke(args.run_dir))
    finally:
        reset_current_identity(identity)
    text = report(run)
    print(text)

    # Imported here so this model-free CLI does not load the probe lane's httpx/yaml/judge
    # machinery.
    from chemclaw.cli.live_probes import run_output_dir

    # A directory per run, so reports never overwrite each other or a tracked file.
    destination = args.report or run_output_dir("durable") / "durable-smoke.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text + "\n", encoding="utf-8")
    print(f"\nwritten to {destination}")
    return 0 if run.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
