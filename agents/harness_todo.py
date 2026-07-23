"""Bridge a submitted async job to the harness's todo list (BACKLOG.md F3-T3 follow-up: D-040).

When the harness's execute-mode loop calls a fire-and-forget tool like `submit_qm_job`, the todo
the model was working cannot simply stay open — `todos_remaining` would keep re-invoking the model
every loop iteration with nothing new to report, and the model has no way to tell "the job is still
running" from "this was forgotten". `mark_awaiting_job` records, directly in the harness's own
`TodoProvider` state, that a todo is blocked on a specific job id; `complete_awaiting_job` flips it
once the job's push-back event arrives (`agents.session_events`), so the *next* turn's
`todos_remaining` check sees it as done instead of stuck open forever.

This closes exactly the gap `BACKLOG.md` names ("flipping the harness `awaiting` todo on
completion — needs MAF TodoProvider store mutation"). It does not attempt to resume the *same*
streamed turn while the job is still running — deciding how a new turn gets triggered server-side
with no client request in flight is a separate, open design question
(`docs/harness-konzept.md` §4) left for when the harness loop is exercised live, not guessed at
here; the flipped todo is picked up the next time the session's loop runs.

`TodoItem` (MAF) carries only `id`/`title`/`description`/`is_complete` — no field for an arbitrary
job id — so the link is a description-string convention. It is never model-authored: this module
creates the "awaiting" todo itself (`mark_awaiting_job` is called from the tool right after it gets
a job id back from Temporal), so the match is exact-string, not inferred from free text the LLM
might get wrong.
"""

from agent_framework import DEFAULT_TODO_SOURCE_ID, AgentSession, TodoItem, TodoSessionStore

_AWAITING_PREFIX = "awaiting-job:"

_store = TodoSessionStore()


def _awaiting_marker(job_id: str) -> str:
    """The exact-match todo description that marks a todo as waiting on `job_id`."""
    return f"{_AWAITING_PREFIX}{job_id}"


async def mark_awaiting_job(
    session: AgentSession, job_id: str, *, title: str, source_id: str = DEFAULT_TODO_SOURCE_ID
) -> None:
    """Add a todo item recording that `job_id` is running, so the plan visibly waits on it."""
    items, next_id = await _store.load_state(session, source_id=source_id)
    items.append(TodoItem(id=next_id, title=title, description=_awaiting_marker(job_id)))
    await _store.save_state(session, items, next_id=next_id + 1, source_id=source_id)


async def complete_awaiting_job(
    session: AgentSession, job_id: str, *, reason: str, source_id: str = DEFAULT_TODO_SOURCE_ID
) -> bool:
    """Mark the todo awaiting `job_id` complete with `reason`; returns whether one was found.

    A no-op (returns `False`) when no open todo is waiting on this job id — e.g. the harness was
    not enabled for the turn that submitted it, or the live session was evicted from the front
    door's in-process cache (`service.app._LiveSessions`) before the job finished. Already-complete
    todos are never matched, so a duplicate push-back for the same job id cannot reopen or
    re-complete one.
    """
    items, next_id = await _store.load_state(session, source_id=source_id)
    marker = _awaiting_marker(job_id)
    found = False
    updated_items: list[TodoItem] = []
    for item in items:
        if not item.is_complete and item.description == marker:
            updated_items.append(
                TodoItem(id=item.id, title=item.title, description=reason, is_complete=True)
            )
            found = True
        else:
            updated_items.append(item)
    if found:
        await _store.save_state(session, updated_items, next_id=next_id, source_id=source_id)
    return found
