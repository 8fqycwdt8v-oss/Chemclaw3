"""Archived Temporal histories, and the replay that checks today's code still accepts them.

A redeploy that changes a workflow's command sequence meets histories the previous version
wrote; the background worker deploys `Recreate`, so one code version inherits every unfinished
run. A history recorded from a released shape and committed here is an ordinary fixture, so the
check runs in `make test` with no live broker.

- `fixtures/histories/` — histories today's code must replay clean.
- `fixtures/histories/superseded/` — one history today's code is known to diverge from, proving
  the control still detects a divergence.

Activities are stubs: a history records what the workflow asked for, never an activity's body.

Re-recording is a decision: gate the change with `workflow.patched`, or re-record and say why no
run of the old shape can still be in flight (for `TemplateWorkflow`, its
`template_run_timeout_seconds` execution timeout).

To re-record, with a broker up (`make up`):

    uv run python tests/recorded_workflow_histories.py
"""

import asyncio
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from temporalio.client import WorkflowHistory

_HISTORY_DIR = Path(__file__).parent / "fixtures" / "histories"
_SUPERSEDED_DIR = _HISTORY_DIR / "superseded"

# How long one replay may take. A divergence can hang the replayer instead of failing it: with
# `failure_exception_types=[Exception]` (as both wrappers here are), `NondeterminismError` becomes
# a failure command a closed history cannot accept, and the replayer re-queues the run forever.
REPLAY_DEADLINE_SECONDS = 60.0

# The SDK's own words for that eviction, logged on `temporalio.worker._workflow` at DEBUG. Watching
# for them turns the hang into a prompt failure; the deadline is the backstop if upstream rewords
# them.
_NONDETERMINISM_MARKERS = ("NonDeterministicError", "TMPRL1100")


@dataclass(frozen=True)
class ArchivedHistory:
    """One committed history: where it came from, and what it is for."""

    path: Path
    workflow_type: str
    history: WorkflowHistory

    def __str__(self) -> str:
        """Name the fixture by file, so a parametrised failure says which one broke."""
        return self.path.name


def _load_dir(directory: Path) -> Iterator[ArchivedHistory]:
    """Read every history in one directory, taking each one's workflow type from the history."""
    for path in sorted(directory.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        # The type is read off `WorkflowExecutionStarted` rather than parsed out of the filename:
        # a fixture that disagreed with its own name would be replayed against the wrong class and
        # fail for a reason that is not the one this control is about.
        started = raw["events"][0]["workflowExecutionStartedEventAttributes"]
        yield ArchivedHistory(
            path=path,
            workflow_type=started["workflowType"]["name"],
            history=WorkflowHistory.from_json(path.stem, raw),
        )


def archived_histories() -> list[ArchivedHistory]:
    """Every history today's code must still replay clean."""
    return list(_load_dir(_HISTORY_DIR))


def superseded_histories() -> list[ArchivedHistory]:
    """Every history today's code is known — and asserted — to diverge from."""
    return list(_load_dir(_SUPERSEDED_DIR))


class _NondeterminismWatcher(logging.Handler):
    """Trip an event the moment the SDK says it evicted a run for non-determinism.

    A handler rather than `caplog`, because the point is to *stop waiting*, not to inspect
    afterwards: the replay that produced this record is never going to return.
    """

    def __init__(self) -> None:
        """Start armed and unfired, holding the loop the waiter will be parked on."""
        super().__init__(level=logging.DEBUG)
        self.tripped = asyncio.Event()
        self.reason = ""
        self._loop = asyncio.get_running_loop()

    def emit(self, record: logging.LogRecord) -> None:
        """Record the first non-determinism message and wake whoever is waiting on it.

        Woken through `call_soon_threadsafe` because the SDK may log from another thread, and
        `asyncio.Event.set` off-loop does not wake a loop parked in `select()`.
        """
        message = record.getMessage()
        if not self.tripped.is_set() and any(m in message for m in _NONDETERMINISM_MARKERS):
            self.reason = message
            self._loop.call_soon_threadsafe(self.tripped.set)


async def replay_failure(archived: ArchivedHistory, workflow_class: type) -> str:
    """Replay one archived history against today's code; return "" if it replayed clean.

    Returns the divergence rather than raising, because callers assert both that it is empty and
    that it is not.
    """
    # Imported here rather than at module scope: constructing a `Replayer` builds an SDK bridge
    # worker, and this module is imported by the test collector on every run, including the ones
    # that never replay anything.
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.worker import Replayer

    watcher = _NondeterminismWatcher()
    sdk_logger = logging.getLogger("temporalio.worker._workflow")
    previous_level = sdk_logger.level
    sdk_logger.setLevel(logging.DEBUG)
    sdk_logger.addHandler(watcher)
    try:
        replayer = Replayer(workflows=[workflow_class], data_converter=pydantic_data_converter)
        replay = asyncio.ensure_future(replayer.replay_workflow(archived.history))
        detected = asyncio.ensure_future(watcher.tripped.wait())
        done, pending = await asyncio.wait(
            {replay, detected},
            timeout=REPLAY_DEADLINE_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        # Cancelled and not awaited: a replay abandoned mid-divergence is parked in the SDK's bridge
        # poll and ignores cancellation, so awaiting would reintroduce the hang. Each caller's own
        # `asyncio.run` tears the loop down.
        for task in pending:
            task.cancel()
        # The watcher is asked first: with `FIRST_COMPLETED`, both futures can be done in one cycle,
        # and a replay that completed without raising must not override a logged divergence.
        # `reason` rather than `tripped` because `emit` sets it synchronously, while the event is
        # only scheduled.
        if watcher.reason:
            return watcher.reason
        if replay in done:
            error = replay.exception()
            return "" if error is None else f"{type(error).__name__}: {error}"
        if detected in done:
            return watcher.reason
        return (
            f"replay of {archived} did not finish within {REPLAY_DEADLINE_SECONDS}s and the SDK "
            "logged no non-determinism; see `REPLAY_DEADLINE_SECONDS`"
        )
    finally:
        sdk_logger.removeHandler(watcher)
        sdk_logger.setLevel(previous_level)


# --------------------------------------------------------------------------------------------
# Recording. Everything below runs only from `__main__`, against a live broker.
# --------------------------------------------------------------------------------------------


def _probe_template() -> Any:
    """A one-step template: the smallest thing that has a tool step and therefore a history."""
    from chemclaw.templates.manifest import Template

    return Template.model_validate(
        {
            "name": "replay-probe",
            "summary": "One tool step, recorded so a later version has something to replay.",
            "inputs": [{"name": "smiles", "type": "string", "description": "The molecule."}],
            "steps": [
                {
                    "id": "screen",
                    "kind": "tool",
                    "purpose": "Stands in for any tool step; the body never reaches history.",
                    "tool": "screen_hazards",
                    "arguments": {"smiles": "${inputs.smiles}"},
                }
            ],
        }
    )


async def _record() -> None:
    """Record both `TemplateWorkflow` endings against a live broker and write them to disk."""
    from temporalio import activity
    from temporalio.client import Client
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.worker import Worker

    from chemclaw.core.config import settings
    from chemclaw.durable.job_record import JobRecord
    from chemclaw.durable.notify import SessionEventInput
    from chemclaw.durable.template_activities import ToolStepInput
    from chemclaw.durable.template_job import TemplateRunInput, TemplateWorkflow

    queue = "history-fixture-queue"
    failing = "fail"

    @activity.defn(name="run_tool_step")
    async def run_tool_step_stub(step: ToolStepInput) -> Any:
        if step.arguments.get("smiles") == failing:
            # A `ValueError` is non-retryable under `BAD_DATA_RETRY`, so the failure fixture
            # records one attempt rather than five.
            raise ValueError("recorded failure fixture")
        return {"ok": True, "tool": step.tool}

    @activity.defn(name="record_job")
    async def record_job_stub(record: JobRecord) -> None:
        return None

    @activity.defn(name="record_session_event_activity")
    async def record_session_event_stub(event: SessionEventInput) -> None:
        return None

    client = await Client.connect(settings.temporal_address, data_converter=pydantic_data_converter)
    # Annotated because `@activity.defn` erases the callables' signatures to bare `function`,
    # which `Worker` will not take under `--strict`.
    activities: list[Callable[..., Any]] = [
        run_tool_step_stub,
        record_job_stub,
        record_session_event_stub,
    ]
    async with (
        Worker(client, task_queue=queue, workflows=[TemplateWorkflow], activities=activities),
        # The two writes at the end of a run are dispatched to the background queue by name, so
        # recording needs a worker there too or the fixture stops one event short of the ending.
        Worker(client, task_queue=settings.background_task_queue, activities=activities),
    ):
        for case, smiles in (("completed", "CCO"), ("failed", failing)):
            workflow_id = f"history-fixture-template-{case}"
            handle = await client.start_workflow(
                TemplateWorkflow.run,
                TemplateRunInput(
                    template=_probe_template(),
                    inputs={"smiles": smiles},
                    requested_by="history-fixture@example.com",
                    roles=["chemist"],
                    session_id="history-fixture-session",
                ),
                id=f"{workflow_id}-{int(asyncio.get_running_loop().time() * 1000)}",
                task_queue=queue,
            )
            try:
                await handle.result()
            except Exception as exc:  # the failure fixture is meant to fail
                print(f"{case}: run failed as recorded ({type(exc).__name__})")
            history = json.loads((await handle.fetch_history()).to_json())
            path = _HISTORY_DIR / f"TemplateWorkflow-{case}.json"
            path.write_text(json.dumps(history, indent=1) + "\n", encoding="utf-8")
            print(f"wrote {path} ({len(history['events'])} events)")


if __name__ == "__main__":
    asyncio.run(_record())
