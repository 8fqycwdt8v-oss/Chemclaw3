"""Job launches announced to the turn that is streaming (plan F2-T3 follow-up, ADR D-042).

`JobStartedEvent` exists in the turn event contract and the web surface renders it, but nothing
emitted it: a chemist who asked for a calculation saw silence between their message and the answer,
with the first sign of the job arriving only as the push-back `job_completed` event (F3-T3). The
tool that launches the job is several layers below the runner that owns the stream, and the job id
is not part of the tool's return path the runner can see, so the launch is announced *ambiently*
for the current turn — the same carrier and the same reasoning as `agents.session_context`: a
contextvar is task-local (concurrent turns never see each other's jobs) and absent off the request
path (the CLI, tests, a worker), where announcing to nobody is simply a no-op.

A plain list, not a queue: the runner drains it between streamed updates, so nothing ever awaits
on it, and a sink that is never drained (an aborted turn) is discarded with its contextvar.
"""

from contextvars import ContextVar

_started_jobs: ContextVar[list[str] | None] = ContextVar("chemclaw_started_jobs", default=None)


def set_job_sink() -> object:
    """Open a sink collecting jobs launched during this turn; returns a `reset_job_sink` token."""
    return _started_jobs.set([])


def reset_job_sink(token: object) -> None:
    """Close the turn's sink, restoring the previous one (turn teardown).

    On client disconnect (GeneratorExit) the coroutine is resumed in a different
    asyncio Context, so the Token was created in a different Context and `reset()`
    raises ValueError. Swallow it — the contextvar lifetime ends with the task
    anyway, so not resetting is harmless (ISSUE-B-11).
    """
    try:
        _started_jobs.reset(token)  # type: ignore[arg-type]
    except ValueError:
        pass


def announce_job_started(job_id: str) -> None:
    """Record that `job_id` was just launched, for the streaming turn to surface.

    A no-op when no sink is open (no turn is streaming — the CLI, a scheduled worker, tests), so a
    job-launching tool can call this unconditionally without knowing who, if anyone, is watching.
    """
    sink = _started_jobs.get()
    if sink is not None:
        sink.append(job_id)


def drain_started_jobs() -> list[str]:
    """Return the jobs launched since the last drain, clearing them (empty when none/no sink)."""
    sink = _started_jobs.get()
    if not sink:
        return []
    drained = list(sink)
    sink.clear()
    return drained
