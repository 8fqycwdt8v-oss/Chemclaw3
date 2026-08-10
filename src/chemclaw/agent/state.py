"""The conversation graph's typed state — what MAF kept in an opaque bag (M5, D-2026-08-10).

MAF's harness held its plan, its mode and its bookkeeping in `session.state`, a dict keyed by
strings nobody declared. Two of this migration's findings come straight out of that: the loop cap
had to *infer* whether it had fired because nothing recorded it, and a todo waiting on a durable job
was marked by prefixing its `description` with `awaiting-job:` — a convention that existed only
because `TodoItem` had no field to put it in.

Here each of those is a named field with a declared type, which is what makes the rest of the
rebuild cheap rather than clever.

**Extends `PlanningState`, not `AgentState`.** `TodoListMiddleware` declares `todos` and the
`write_todos` tool that maintains them, so the plan itself is already typed by the middleware that
owns it; adding a second list beside it would give the graph two answers to "what is the plan".

**`awaiting_jobs` replaces the marker convention.** A durable launcher records that agreed work is
in flight, and the plan gate must not count that as a change to the plan — an approved plan that
revoked its own approval the first time it started a job would be unusable. Under MAF the two were
told apart by a string prefix on a description field; here they are simply not the same field, so
the exclusion the gate needs is structural rather than a parse.
"""

from langchain.agents.middleware.todo import PlanningState


class ChemclawState(PlanningState):
    """The graph state Chemclaw adds on top of the plan the todo middleware maintains.

    Fields arrive with the phase that reads one — a declared field nothing consults is the same
    stub as a function nothing calls, and reads as coverage while proving nothing.
    """

    # Durable job ids this turn is waiting on. Not todos: they are bookkeeping *about* work a human
    # already approved, so `plan_identity` must not see them (see the module docstring).
    awaiting_jobs: list[str]
