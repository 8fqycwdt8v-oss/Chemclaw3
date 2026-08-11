"""The conversation graph's typed state — what MAF kept in an opaque bag (M5, D-2026-08-10).

MAF's harness held its plan, its mode and its bookkeeping in `session.state`, a dict keyed by
strings nobody declared. Two of this migration's findings come straight out of that: the loop cap
had to *infer* whether it had fired because nothing recorded it, and a todo waiting on a durable job
was marked by prefixing its `description` with `awaiting-job:` — a convention that existed only
because `TodoItem` had no field to put it in.

The first is a named field with a declared type here, which is what makes the rest of the rebuild
cheap rather than clever. The second turned out not to need one at all — see below.

**Extends `PlanningState`, not `AgentState`.** `TodoListMiddleware` declares `todos` and the
`write_todos` tool that maintains them, so the plan itself is already typed by the middleware that
owns it; adding a second list beside it would give the graph two answers to "what is the plan".

**The marker convention is gone and nothing replaced it here**, which is the whole fix. The gate
must not count "a job this plan agreed to is now in flight" as a change to the plan — an approved
plan that revoked its own approval the first time it started a job would be unusable — and under
MAF that took a filter, because the bookkeeping lived in the same list as the plan. Now it does not
live there at all: a launched job is a `job_records` row and a `session_events` push-back, so
`todos` holds the plan and only the plan, and the exclusion the gate needs is structural rather
than a parse.

An `awaiting_jobs: list[str]` field was declared here for that job before the durable side was
built, and the durable side went to the two stores above instead. Nothing ever wrote it or read it,
while three docstrings — this one, `plan_gate.enforce_plan_approval`'s and a test's — described it
as the mechanism. It is removed rather than filled in, by the rule immediately below: a declared
field nothing consults reads as coverage while proving nothing, and prose about it reads as a
design somebody can rely on.
"""

from langchain.agents.middleware.todo import PlanningState


class ChemclawState(PlanningState):
    """The graph state Chemclaw adds on top of the plan the todo middleware maintains.

    Fields arrive with the phase that reads one — a declared field nothing consults is the same
    stub as a function nothing calls, and reads as coverage while proving nothing.
    """

    # How many model calls this turn has made — the runaway guard's counter (`agent/loop_cap.py`).
    # A field rather than a framework internal because the whole defect being fixed is that MAF's
    # cap fired where nothing could observe it.
    model_calls: int
