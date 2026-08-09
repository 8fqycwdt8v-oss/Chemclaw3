# Workflow versioning policy

Temporal replays a workflow's **code** against its recorded **history**. If a run is in flight when
a deploy changes that code's control flow, the replay produces a different command sequence than the
history records and the run fails with a nondeterminism error — after the fact, on a workflow nobody
is watching, with a stack trace that points at the new code rather than at the deploy that broke it.

This policy exists so that never happens silently. It is a checklist, deliberately not a CI guard —
see the last section for why.

## Today's state (read this first)

**No live Temporal cluster holds Chemclaw histories yet** (the F4/F5/F6 live edges are still open,
see `docs/planning/BACKLOG.md`). Every workflow-logic change made so far — `fan_out`'s local activity, the
`ElnSyncWorkflow` chunk loop, the BO activities' seed argument, the `resolve_notes_per_run` activity
added to the three memory synthesis workflows, and the `plan_document_sync` activity now carrying
`DocumentShareSyncWorkflow`'s continue-as-new bound — was therefore safe **and needs no
retroactive `workflow.patched()` gate**: there is no history to replay against. Gating them now
would add permanent branches guarding a case that cannot occur.

Note what the last two have in common, because it is the pattern this policy is really about: both
moved a *count* out of workflow code and into an activity. A bound read live from `settings` makes
the command sequence a function of the replaying worker's configuration rather than of history, so
a redeploy that lowers it is itself a workflow-logic change — without touching a line of workflow
code. Neither of those is gated for the reason above; from the first production deploy onward, a
change to either value would be.

From the **first production deploy** onward, that changes: every deploy that touches workflow code
must go through the checklist below.

## What counts as a workflow-logic change

The rule is not "did the file change" but "would a replay issue different commands, in a different
order". Changes that **need** gating or draining:

- adding, removing, or reordering `execute_activity` / `execute_child_workflow` / `start_activity`
  calls, including inside a loop or a conditional;
- changing an activity's **arguments** or its **name/type** (a renamed workflow or activity class is
  a different command in the history — this is why the `QMJobWorkflow` → `CalculationWorkflow`
  rename is dropped, not deferred);
- adding, removing, or changing the duration of a `workflow.sleep` / timer, or a signal/query/update
  handler's control flow;
- changing loop bounds or branch conditions that decide how many commands are issued
  (e.g. the sync's chunk size, a fan-out's batch size).

Changes that are **safe** without gating, because they are not part of the replayed command stream:

- an **activity body** (activities are not replayed — only their scheduling is);
- docstrings, comments, logging, type hints, variable names;
- pure helper functions called *outside* the workflow (in an activity, or at import time);
- anything in `service/`, `agents/`, `calc/`, `kg/`, `memory/`, `report/`, `sources/` that no
  workflow calls inside its `@workflow.run` body.

When unsure, treat it as a logic change. The cost of an unnecessary patch gate is one branch; the
cost of a missed one is a failed production run.

## The two sanctioned responses

### 1. Gate with `workflow.patched()` (default — no deploy coordination needed)

```python
if workflow.patched("eln-sync-chunked-fetch"):
    result = await workflow.execute_activity(fetch_chunk, ...)   # new path
else:
    result = await workflow.execute_activity(fetch_all, ...)     # old path, for in-flight runs
```

- Pick a **stable, descriptive patch id**; it is written into history and must never be reused for a
  different change.
- Once every run started before the deploy has completed, replace the branch with
  `workflow.deprecate_patch("eln-sync-chunked-fetch")` in one deploy, then delete it in the next.
  Leaving patch branches forever is how workflow code becomes unreadable.
- Cheap for short-lived runs (QM jobs, memory synthesis, report sections) — these drain in minutes
  to hours.

### 2. Drain in-flight runs, then deploy (for a change too invasive to branch)

Make draining an **explicit deploy step**, not an assumption:

1. Pause the Temporal Schedules that start new runs (`src/chemclaw/durable/schedules.py` ids; the
   ELN sync, memory jobs, and eval drift are schedule-driven).
2. Wait until the affected workflow types have no open executions
   (Temporal UI at `:8081` → Workflows → filter by type, status Running).
3. Deploy the new image and roll the workers.
4. Resume the Schedules.

Long-running workflows are the ones to watch here: `InteractionApprovalWorkflow` holds a human
approval for up to `interaction_approval_timeout_seconds` (default **7 days**), so "wait for it to
drain" is not a coffee break — such a change wants a patch gate, or an explicit decision to
terminate and re-drive the pending holds.

## Deploy checklist (add to the release ticket)

- [ ] Does this diff touch any `@workflow.defn` class body, or a helper called from one?
- [ ] If yes: is each logic change either `workflow.patched()`-gated, or covered by a drain step?
- [ ] Are any patch ids from an earlier deploy now drainable (→ `deprecate_patch`, then delete)?
- [ ] Did any workflow **type name** or activity name change? (If so, stop: rename via a new type
      plus a migration window, never by renaming a class in place.)

## Why there is no CI guard for this

A guard that fails a PR when `workflows/*.py` changed without a `workflow.patched()` call cannot
tell a docstring edit from a reordered activity call — the repo's own history is mostly the former.
It would fire on nearly every PR, and a check that is wrong most of the time teaches people to
bypass it, taking the real signal with it. A checklist a human reads at deploy time is weaker in
theory and stronger in practice. Revisit if a real nondeterminism incident shows the checklist
being skipped.
