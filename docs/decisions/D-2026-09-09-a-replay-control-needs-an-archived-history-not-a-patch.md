# D-2026-09-09-a-replay-control-needs-an-archived-history-not-a-patch — the background worker deploys `Recreate`, so the new image must replay the old one's histories; that is checked against committed histories, and `workflow.patched` is declined

**Status:** accepted · **Date:** 2026-09-09 · **Builds on:**
D-2026-08-27-what-a-second-background-worker-would-race-on (`replicas: 1` and `Recreate`, and why
an overlap is the thing to avoid), D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose (a
control belongs in a test, not in a docstring) · **Closes** the check
`durable/connector_job.py` has asked for in prose since it was written, and **declines** two of the
three fixes a reviewer proposed for it.

## Context

`deploy/helm/chemclaw/templates/deployment-workers.yaml` deploys core's background worker with
`strategy: Recreate` and one replica. That is deliberate and is argued in
`D-2026-08-27-what-a-second-background-worker-would-race-on`: two generations polling
`background-jobs` at once is a corpus race, because `NoteReindexWorkflow` retires `note_index` rows
against *this pod's* knowledge checkout.

The consequence nobody had written down is the mirror image of it. With no overlap, every
unfinished run on that queue is resumed by exactly **one** code version — the new one — so the new
image must be able to replay the histories the old one wrote. Nothing in this repository checked
that. `grep -rn "patched|get_version|build_id|use_worker_versioning" src/` returns zero hits across
25 `@workflow.defn` classes, and `.github/workflows/` and `Jenkinsfile` contain no `Replayer`.

`ConnectorJobWorkflow.__init__`'s docstring already named the missing control in its own words —
*"Detecting a code-versus-history mismatch needs an archived history, which is a CI job rather than
a unit test"* — and it was never built.

## What was measured

**The divergence is real.** The pre-change tree (`c9122f64^`, before `TemplateWorkflow` gained
`execute_activity(record_job)` on both endings) was extracted with `git archive`, a one-tool-step
template run was executed against the live broker with that code, and its history was replayed by
both trees:

```
control  (the recording code replays its own history): REPLAY OK
measured (today's code replays that history):
  [TMPRL1100] Nondeterminism error:
    Activity machine does not handle this event: HistoryEvent(id: 11, WorkflowExecutionCompleted)
  REPLAY FAILED: TimeoutError
```

**Its blast radius is smaller than the finding claimed, and that is the load-bearing measurement.**
"Every in-flight workflow is resumed by the other generation" is true; "so it hangs" does not
follow for a change of *this shape*. Temporal only compares a command against history at a position
history already records. Both `record_job` dispatches are appended at the *end* of the run, so an
unfinished run meets them past its own tip, where they are new commands rather than replayed ones.
Driven as a real handover — generation N started the run and parked it on an unclaimed activity,
then went away; generation N+1 (today's code) picked it up — the run **completed**:

```
 1 WORKFLOW_EXECUTION_STARTED        identity 28734  (generation N)
 5 ACTIVITY_TASK_SCHEDULED  run_tool_step
 9 WORKFLOW_TASK_TIMED_OUT                           (generation N goes away)
11 WORKFLOW_TASK_STARTED             identity 28788  (generation N+1 resumes)
13 ACTIVITY_TASK_SCHEDULED  record_job               (the *new* command, appended)
19 WORKFLOW_EXECUTION_COMPLETED
```

So the nondeterminism above is a property of a **closed** history, and production never resumes a
closed run. That does not make the hazard imaginary — it relocates it. What breaks an unfinished
run is a change to a command *before* its tip: a reordering, a removal, an insertion earlier in the
sequence. Nothing distinguishes those from an append at review time except a check, and a closed
history is the only artifact that contains the whole sequence.

**The failure mode is a hang, in the detector as well as in production, and the reason is
first-party.** `TemplateWorkflow` and `ConnectorJobWorkflow` both carry
`@workflow.defn(failure_exception_types=[Exception])`, added by REV-13 precisely so a plain
exception fails a run instead of parking it. `NondeterminismError` is an `Exception`, so the SDK
tries to emit a workflow-*failure* command — which a closed history cannot accept — and evicts and
re-queues forever. Measured on the identical history and code:

```
failure_exception_types=[Exception]  (as shipped): never returned
failure_exception_types=[]                       : NondeterminismError, < 1 s
```

A replay control that does not know this hangs CI instead of failing it.

## Decision

**1. Archived histories, committed, replayed by the ordinary suite.**
`tests/fixtures/histories/` holds histories today's code must replay clean;
`tests/recorded_workflow_histories.py` records them against a live broker with stub activities (an
activity's body never appears in a history, so recording needs no RDKit, no model and no database);
`tests/test_workflow_replay.py` replays them.

The docstring that asked for this named the wrong obstacle. What a self-recorded history lacks is
*age*, not a runner — its point about code agreeing with itself by construction is exactly right,
and the fix is to commit a history from a released shape, not to move the check to CI. So there is
**no new CI job**: this runs in `make test`, with no broker.

**2. The control is asserted against a divergence it must detect.**
`tests/fixtures/histories/superseded/` holds the pre-`record_job` history above, and the suite
asserts it comes back as a divergence. A replay check that silently stopped detecting anything
would otherwise stay green forever, which is the failure `reject_widening` and
`map_to_hpc_identity` are this repository's standing examples of.

**3. The gap is declared, not implied.** One of core's 22 background workflows has a fixture.
`UNCOVERED_BACKGROUND_WORKFLOWS` names the other 21, so adding a workflow forces a decision about
its history and nobody reads "there is a replay check" as "the workflows are covered".

**4. `strategy: Recreate` stays**, and its comment now says what it costs. It is not the cause of
the hazard; it is what makes the hazard *decidable*. Under a rolling update two generations
interleave and a divergence appears and disappears with whichever pod won the poll. Under
`Recreate` the question is "does one code version accept the histories the previous one wrote",
which is exactly the question a committed fixture answers.

## What was not taken

**`workflow.patched` on the two `_record_run` call sites — declined.** It would gate a change that
is measured *not* to break an unfinished run (the handover above completed and recorded), so its
recipient set is empty: a history old enough to diverge is a history whose run is closed. Against
that, a patch is permanent scaffolding: Temporal's own guidance is to deprecate and remove it a
release later, so a patch that ships without a named owner and a removal release never gets
removed. Neither can be named here, because there is nothing for it to protect. The general rule
this leaves behind is the one worth keeping: **a change that appends commands at the end of a
workflow needs no patch; a change that alters an earlier command needs one, and
`tests/test_workflow_replay.py` is what tells the two apart.**

**Worker versioning / build IDs — declined for this deployment, on its own constraint.** Pinning an
in-flight run to the build id that started it is the structural answer to code-versus-history
mismatch, and it requires the old worker to stay up until its runs drain. A `TemplateWorkflow` run
lives up to `template_run_timeout_seconds` — 45,330 s, 12.6 h at the shipped default — so draining
means two background workers for half a day, which is exactly the overlap
`D-2026-08-27-what-a-second-background-worker-would-race-on` pins `replicas: 1` to prevent. The
two controls are in direct conflict and the corpus race is the one with a measured data-loss path.
Re-opening this needs `NoteReindexWorkflow`'s host-local checkout to stop being host-local first.

**Recording a fixture for all 22 — not now.** Twenty-one of them need infrastructure this
repository does not have offline (a connector bundle's child workflow, a git checkout, a share
mount). Faking them would produce histories that are not the shipped shape, which is worse than
none. The declared list is the backlog.

## Consequences

- A change to a workflow's command sequence now turns `make test` red with the SDK's own
  description of the divergence, e.g. *"Activity type of scheduled event 'record_job' does not
  match activity type of activity command 'record_session_event_activity'"*.
- **Re-recording a fixture is a decision, not a refresh.** Red means the current code no longer
  accepts a history the shipped code wrote. The two honest responses are to gate the change with
  `workflow.patched`, or to re-record *and* state why no run of the old shape can still be in
  flight — for `TemplateWorkflow` that is bounded by `template_run_timeout_seconds`. Re-recording
  without asking turns the control into a copy of whatever was committed last.
- The check is a deliberately conservative proxy: a closed history covers positions an unfinished
  run would never replay, so a red result is a question about *where* the sequence diverges, not
  automatically an outage.
- `durable/connector_job.py`'s docstring is corrected in place — the paragraph's argument stands
  and only its conclusion ("a CI job") was wrong, so it now says so and names itself as one of the
  21 uncovered.
