"""Archived Temporal histories, and the replay that checks today's code still accepts them.

**A workflow's history is a contract with every later version of its own code.** Temporal replays
a run from its history on whatever worker picks it up, so a redeploy that changes the *sequence of
commands* a workflow issues meets histories the previous version wrote. The background worker is
the sharp case: `deployment-workers.yaml` deliberately deploys it `Recreate` (one replica, no
overlap — `D-2026-08-27-what-a-second-background-worker-would-race-on`), so after the cut there is
exactly one code version and it is handed every unfinished run on `background-jobs`.

`durable/connector_job.py` already argued why the suite could not hold this: a test that runs a
workflow and then replays the history it just produced compares code against a history that same
code wrote, so the two agree by construction. Its conclusion — "a CI job rather than a unit test" —
named the wrong obstacle. What a self-recorded history lacks is *age*, not a runner. An **archived**
history, recorded from a released shape and committed beside the code, is an ordinary fixture, so
this runs in `make test` with everything else and needs no separate job and no live broker.

**Two directories, two opposite assertions.**

- `fixtures/histories/` — histories today's code must replay clean. Recorded from the shape this
  repository ships; the next change to a workflow's command sequence is what they catch.
- `fixtures/histories/superseded/` — one history today's code is *known* to diverge from, kept so
  the control can prove it detects a divergence at all. Without it, a replay check that silently
  stopped detecting anything would stay green forever.

**The activities are stubs, and that costs nothing.** A history records what the *workflow* asked
for; an activity's body never appears in it. Recording therefore needs no RDKit, no model and no
database, only the real workflow code against a real broker.

**Re-recording is a decision, not a refresh.** A fixture going red means the current code no longer
accepts a history the shipped code wrote. The two honest responses are to gate the change with
`workflow.patched`, or to re-record *and* say why no run of the old shape can still be in flight —
for `TemplateWorkflow` that is `template_run_timeout_seconds` (12.6 h at the shipped default), the
execution timeout `templates/registry.py` starts every run with. Re-recording without asking is how
this control would come to certify only itself.

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

# How long one replay may take before the control gives up on it. A clean replay of the shipped
# fixtures measures well under a second of actual work; the rest is the sandbox importing the
# workflow module. The bound exists because **a divergence can hang the replayer instead of
# failing it**, which is the second thing this ADR measured: a workflow declared
# `failure_exception_types=[Exception]` (both wrappers here are, deliberately — REV-13) converts
# *any* exception raised in workflow code into a workflow-failure *command*, and
# `NondeterminismError` is an exception. A closed history cannot accept that command, so the
# replayer evicts the run and re-queues it forever. Measured on 2026-09-09: identical history and
# code, `failure_exception_types=[Exception]` never returned; emptied, it raised
# `NondeterminismError` in under a second.
REPLAY_DEADLINE_SECONDS = 60.0

# The SDK's own words for that eviction, logged on `temporalio.worker._workflow` at DEBUG. Watching
# for it is what turns the hang above into a prompt, well-named failure instead of a 60 s wait; the
# deadline stays as the backstop for the day upstream rewords this, so a reworded message costs the
# control its error text and not its teeth.
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

        Woken through `call_soon_threadsafe` because the SDK may log this from its workflow-task
        executor thread, and `asyncio.Event.set` called off-loop sets the flag without waking a
        loop already parked in `select()` — which would leave this watcher correct and useless,
        detecting the divergence and still waiting out the deadline.
        """
        message = record.getMessage()
        if not self.tripped.is_set() and any(m in message for m in _NONDETERMINISM_MARKERS):
            self.reason = message
            self._loop.call_soon_threadsafe(self.tripped.set)


async def replay_failure(archived: ArchivedHistory, workflow_class: type) -> str:
    """Replay one archived history against today's code; return "" if it replayed clean.

    Returns the divergence rather than raising it, because both assertions this module serves need
    the *answer* — one that it is empty, one that it is not — and a helper that raised would make
    the second one read as an expectation of failure rather than as a check that the control works.
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
        # Cancelled and **not awaited**, which is deliberate and was measured: a replay abandoned
        # mid-divergence is parked in the SDK's bridge poll and does not answer cancellation —
        # `asyncio.gather` on it had not returned after 20 s, so awaiting here would reintroduce
        # exactly the hang this function exists to convert into an answer. Each caller runs this
        # through its own `asyncio.run`, so the loop closing behind it is the teardown.
        for task in pending:
            task.cancel()
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
