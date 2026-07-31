"""Bridge a submitted async job to the harness's todo list (BACKLOG F3-T3 follow-up: D-040).

When the harness's execute-mode loop calls a fire-and-forget tool like `compute_dft_energy`, the
todo the model was working cannot simply stay open — `todos_remaining` would keep re-invoking the
model every loop iteration with nothing new to report, and the model has no way to tell "the job is
still running" from "this was forgotten". `mark_awaiting_job` records, directly in the harness's
own `TodoProvider` state, that a todo is blocked on a specific job id; `complete_awaiting_job`
flips it once the job's push-back event arrives (`chemclaw.agent.session_events`), so the *next*
turn's
`todos_remaining` check sees it as done instead of stuck open forever.

This closes exactly the gap `docs/planning/BACKLOG.md` names ("flipping the harness `awaiting` todo
on completion — needs MAF TodoProvider store mutation"). It does not attempt to resume the *same*
streamed turn while the job is still running — deciding how a new turn gets triggered server-side
with no client request in flight is a separate, open design question
(`docs/guides/harness-konzept.md` §4) left for when the harness loop is exercised live, not guessed
at here; the flipped todo is picked up the next time the session's loop runs.

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


async def todo_titles(
    session: AgentSession, *, source_id: str = DEFAULT_TODO_SOURCE_ID
) -> list[str]:
    """Return the session's todo list as human-readable `[x]`/`[ ] title` lines (the plan).

    The read side of the same store the two functions above mutate — kept here so every access to
    the harness's todo state goes through one module. Used by the front-door runner to emit
    `PlanEvent`, which is why the rendering is a plain string per item: the surfaces show a
    checklist, and completion state is the one thing they must not have to infer.
    """
    items, _ = await _store.load_state(session, source_id=source_id)
    return [f"[{'x' if item.is_complete else ' '}] {item.title}" for item in items]


async def todo_plan_items(
    session: AgentSession, *, source_id: str = DEFAULT_TODO_SOURCE_ID
) -> list[str]:
    """The plan's *work items* — the titles in order, with neither checkbox nor bookkeeping.

    The identity half of the same read `todo_titles` renders for display, and the two are separate
    because they answer different questions. A surface asks "what does this plan look like right
    now", which must include completion state. An authorization asks "which plan is this", which
    must **not**: an approval bound to a hash that moves the moment a box is ticked cannot survive
    the execution it authorizes, and re-approving after every step is not a control anyone would
    operate (D-167 reverses D-137 on exactly this point).

    Two exclusions, both load-bearing:

    - the checkbox, per above;
    - every todo this system authored itself, identified by the `awaiting-job:` marker
      `mark_awaiting_job` writes. Those appear *during* an approved run — a durable launch adds one
      (`chemclaw.connectors.jobs._mark_awaiting_if_harness`) — so counting them would let an
      approved plan revoke its own approval the first time it started a job. They are also not work
      a human ever agreed to: the launcher created them to record that work already agreed to is in
      flight.

    What remains is exactly the set of items a person read and said yes to, so adding, removing or
    rewording a step is a different plan and is unapproved, while working through the agreed steps
    is not.

    `description` is optional on `TodoItem` and the model routinely omits it, so the `or ""` is the
    ordinary case rather than a defensive flourish.
    """
    items, _ = await _store.load_state(session, source_id=source_id)
    return [
        item.title for item in items if not (item.description or "").startswith(_AWAITING_PREFIX)
    ]


async def complete_awaiting_job(
    session: AgentSession, job_id: str, *, reason: str, source_id: str = DEFAULT_TODO_SOURCE_ID
) -> bool:
    """Mark the todo awaiting `job_id` complete with `reason`; returns whether one was found.

    A no-op (returns `False`) when no open todo is waiting on this job id — e.g. the harness was
    not enabled for the turn that submitted it, or the live session was evicted from the front
    door's in-process cache (`chemclaw.api.app._LiveSessions`) before the job finished.
    Already-complete
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
