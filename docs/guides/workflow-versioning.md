# Workflow versioning policy

Temporal replays a workflow's **code** against its recorded **history**. If a run is in flight when
a deploy changes that code's control flow, the replay produces a different command sequence than the
history records and the run fails with a nondeterminism error — after the fact, on a workflow nobody
is watching, with a stack trace that points at the new code rather than at the deploy that broke it.

This policy exists so that never happens silently. It has two halves: a **replay check in the
ordinary suite** that catches a divergence for the workflows it has a history for, and a **deploy
checklist** for everything that check does not cover.

## Today's state (read this first)

**No production Temporal cluster holds Chemclaw histories yet** — the rollout itself is written and
not run (`docs/planning/BACKLOG.md`, "Push-to-registry + `helm upgrade` rollout, run"). The policy
below applies from the first production deploy; until then a workflow-logic change needs no
retroactive patch gate, because there is no history to replay against.

**Why the background worker makes this sharp.** `deploy/helm/chemclaw/templates/deployment-workers.yaml`
deploys core's background worker with `strategy: Recreate` and one replica (no overlap between
generations), so after a deploy exactly one code version is handed every unfinished run on
`background-jobs`. That code must replay the histories the previous one wrote
(`D-2026-09-09-a-replay-control-needs-an-archived-history-not-a-patch`).

**A bound read live from `settings` inside workflow code is a workflow-logic change waiting to
happen**: it makes the command sequence a function of the replaying worker's configuration rather
than of history, so a redeploy that lowers it changes replay without touching a line of workflow
code. Read such a count in an activity and pass it in (the `ElnSyncWorkflow` chunk loop,
`resolve_notes_per_run` and `plan_document_sync` all do).

## The replay check (in `make test`)

- `tests/fixtures/histories/` holds archived histories today's code must replay clean.
- `tests/fixtures/histories/superseded/` holds a history today's code is *known* to diverge from,
  so the suite proves the check still detects a divergence at all.
- `tests/test_workflow_replay.py` replays both; `UNCOVERED_BACKGROUND_WORKFLOWS` in that file names
  every background workflow that has **no** fixture, so adding a workflow forces a decision about its
  history and nobody reads "there is a replay check" as "every workflow is covered".
- To record a fixture, with a broker up (`make up`): `uv run python tests/recorded_workflow_histories.py`.
  Activities are stubs — an activity's body never appears in a history.

**A fixture going red is a decision, not a refresh.** The two honest responses are to gate the change
with `workflow.patched`, or to re-record *and* state why no run of the old shape can still be in
flight (for `TemplateWorkflow`, `template_run_timeout_seconds`). Re-recording without asking is how
the control would come to certify only itself.

## What counts as a workflow-logic change

The rule is not "did the file change" but "would a replay issue different commands, in a different
order, **at a position an unfinished history already records**". Measured, a command *appended* after
a run's tip is a new command rather than a replayed one, and an unfinished run picks it up and
completes; what breaks it is a change to an *earlier* command
(`D-2026-09-09-a-replay-control-needs-an-archived-history-not-a-patch`). Changes that **need** gating
or draining:

- adding, removing, or reordering `execute_activity` / `execute_child_workflow` / `start_activity`
  calls before the end of the run, including inside a loop or a conditional;
- changing an activity's **arguments** or its **name/type** — a name is a wire name (see below);
- adding, removing, or changing the duration of a `workflow.sleep` / timer, or a signal/query/update
  handler's control flow;
- changing loop bounds or branch conditions that decide how many commands are issued
  (e.g. the sync's chunk size, a fan-out's batch size).

Changes that are **safe** without gating, because they are not part of the replayed command stream:

- an **activity body** (activities are not replayed — only their scheduling is);
- docstrings, comments, logging, type hints, variable names;
- pure helpers called *outside* the workflow (in an activity, or at import time);
- anything in `api/`, `agent/`, `science/`, `kg/`, `memory/`, `retrieval/`, `ingest/` that no
  workflow calls inside its `@workflow.run` body.

When unsure, treat it as a logic change. The cost of an unnecessary patch gate is one branch; the
cost of a missed one is a failed production run.

## The sanctioned responses

### 1. Gate with `workflow.patched()` (default — no deploy coordination needed)

```python
if workflow.patched("awaiting-outbound-delivery"):
    ...   # new path
else:
    ...   # old path, for in-flight runs
```

The tree's live examples are `durable/awaiting.py` (`awaiting-outbound-delivery`),
`durable/hypothesis_tournament.py` (`tournament-empty-calls-refused-before-budget`) and
`durable/digest.py`.

- Pick a **stable, descriptive patch id**; it is written into history and must never be reused for a
  different change.
- Once every run started before the deploy has completed, replace the branch with
  `workflow.deprecate_patch("<id>")` in one deploy, then delete it in the next. Leaving patch
  branches forever is how workflow code becomes unreadable.
- Cheap for short-lived runs (calculation jobs, memory synthesis, report sections) — these drain in
  minutes to hours.

### 2. Rename an activity or workflow in two releases

A registered name is the string a history schedules against, so it is renamed by **keeping the old
name registered** beside the new one for a release, then deleting it once no run that scheduled the
old name is open (`D-2026-09-14-an-activity-name-is-a-wire-name-so-it-is-renamed-in-two-releases`).
The live instance is `durable/report_workflow.py`'s `propose_report` alias of `record_report_note`;
its removal condition is a `BACKLOG.md` row.

### 3. Drain in-flight runs, then deploy (for a change too invasive to branch)

Make draining an **explicit deploy step**, not an assumption:

1. Pause the Temporal Schedules that start new runs (ids in `src/chemclaw/durable/schedules.py`).
2. Wait until the affected workflow types have no open executions — Temporal UI (`:8081` under
   `make up`) → Workflows → filter by type, status Running, or
   `temporal workflow list --query 'WorkflowType = "<Type>" AND ExecutionStatus = "Running"'`.
3. Deploy the new image and roll the workers.
4. Resume the Schedules.

Long-running workflows are the ones to watch: `AwaitAnswerWorkflow` holds an open question for up
to `awaiting_max_days` (default **90 days**) and a `TemplateWorkflow` run lives up to
`template_run_timeout_seconds`, so "wait for it to drain" is not a coffee break — such a change wants
a patch gate, or an explicit decision to terminate and re-drive the pending runs.

Worker versioning (build IDs) is declined for this deployment: it needs the old worker up until its
runs drain, which is the two-worker overlap `replicas: 1` exists to prevent (same ADR).

## Deploy checklist (add to the release ticket)

- [ ] Does this diff touch any `@workflow.defn` class body, or a helper called from one?
- [ ] If yes: is `make test` green on `tests/test_workflow_replay.py`, and is the touched workflow
      in `UNCOVERED_BACKGROUND_WORKFLOWS` (so the check said nothing about it)?
- [ ] Is each logic change before the end of the run either `workflow.patched()`-gated, or covered by
      a drain step?
- [ ] Are any patch ids or renamed-name aliases from an earlier deploy now drainable
      (→ `deprecate_patch`, then delete; or delete the alias)?
- [ ] Did any workflow **type name** or activity name change? If so, it is a two-release rename,
      never a class renamed in place.

## Why there is no diff-based CI guard

A guard that fails a PR when workflow code changed without a `workflow.patched()` call cannot tell a
docstring edit from a reordered activity call — the repo's own history is mostly the former. It
would fire on nearly every PR, and a check that is wrong most of the time teaches people to bypass
it. The replay check above is the guard that *can* tell the two apart, for the workflows it has a
history for; the checklist covers the rest. Revisit if a real nondeterminism incident shows the
checklist being skipped.
