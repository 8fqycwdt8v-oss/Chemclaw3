# D-2026-09-15-an-approval-is-for-one-version-of-one-workflow — a composed workflow may launch a durable job, once a person has said so

**Status:** accepted · **Date:** 2026-09-15 · Revisits
`D-2026-09-15-an-agent-authored-workflow-is-read-only-by-construction`, which refused this and
named the cost in the same breath.

## Context

That decision refused a `job` step in an agent-composed workflow, and stated what it cost rather
than leaving it to be discovered: every durable job launcher is state-changing, so the rankings and
conformer searches these procedures exist to sequence were exactly what a composed one could not
contain. It composed reads and nothing else. Asked to widen it.

The reason the refusal existed is unchanged and is the thing a widening must not break.
`D-2026-08-12` exempts a template's `agent` step from the plan gate *because* a template is
reviewed and uncreatable at run time; a composed workflow is neither, so the premise has to be
restored some other way. A template run has no session, so nothing *inside* it can put a human in
front of a launch.

## Decision

**Move the human to where one exists, and bind what they approve to the version they were shown.**

`POST /workflows/{name}/approval` records that a person approved one composed workflow, keyed on
`template_fingerprint(document)` — the same hash a resumed run already compares.
`composed.unapproved_jobs` withholds the job steps until that approval matches the document as it
stands now.

**One version of one workflow, not an actor.** The phrase this was asked for in was "a standing
per-actor approval for job launches", and per-actor is the broader reading and the wrong one: it
never lapses, so a workflow re-composed into something else inherits the permission granted to what
it used to be. Keyed on the document's hash, **re-composing lapses the approval automatically** —
the stored fingerprint stops matching, with no clearing logic anybody can forget. That is
`plan_approvals`' own shape one layer up, where a rewritten plan is a different key.

**Composing is deliberately not the decision.** A workflow with job steps composes and is stored,
unapproved; the refusal lands at the *run*. Refusing the composition was the first design and is
wrong for a plain reason: a workflow nobody may compose is a workflow nobody can put in front of a
person to approve. What the compose result does instead is say so, at the moment the chemist is
still in the conversation to hear it.

**A route and not a tool**, for the reason `routes/plan.py::decide_plan` gives in as many words: a
model must never be able to authorize its own plan. The guarantee is obtained the way that one's is
— by not building the tool rather than by removing it afterwards — and
`tests/test_api_workflows.py` asserts it over the registered surface by *behaviour*: no tool calls
`approve` or sets the columns. Reading `approved_fingerprint` is allowed and is what
`run_composed_workflow` does, because that read is the enforcement; a scan forbidding it would be
asking the gate not to look at the thing it gates on.

**The approval is not spent by a run**, which is the deliberate difference from a plan approval. A
plan is a thing the agent chose this turn and its approval authorizes that turn; a composed
workflow is a procedure a person keeps, and approving it means "this may run when I ask for it".

**The GET is not decoration.** An approval that names no steps is
`D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool` one layer over: a person
approving a *name* has approved whatever it currently contains. So the read hands back the steps,
the subset that launch jobs, and the fingerprint to post; the POST answers 409 to any other one.

## Where the line is, and why it is there

**An approval lifts a `job` step and lifts nothing else.** `authored_problems` takes no approval
argument at all, which is how that is enforced rather than remembered.

- A **`job` step** is bounded compute *whose call the approver read*: the job and its arguments are
  in the document they approved, and running it produces a result.
- **`write_tools`** is not a call. It is a permission handed to a model turn, spent later on a call
  nobody has seen, chosen by the model inside the step. A person can meaningfully approve the first
  and cannot meaningfully approve the second, so no approval offers to.
- A **side-effecting `tool` step** sits nearer the first and stays refused with it. The reachable
  set is every write in the tree, and the case for widening it has not been made — `BACKLOG.md`
  carries that as its own question rather than smuggling it in beside the one that was asked.

**Why one person and not two.** `api/routes/pending.py`'s `SECOND_PERSON_KINDS` requires that the
requester of an irreversible effect never be its approver, because the whole content of that
control is that a second person looked. That is not this case and the difference is not a
convenience: there, the requester is a human and the second human is the control; here the
*composer is the agent*, so the approver is already the second party. This is `decide_plan`'s
shape — the session's own user approves what the agent proposed — not `pending`'s.

**What the effect ledger still does.** A job declared `irreversible` runs `_approve_effect` inside
the connector wrapper regardless, and on the template path that wait is cut off by the run ceiling
(`connector_job.wrapper_execution_timeout`). The job then fails rather than proceeding, which is
the correct direction and is unchanged by this decision: a standing approval authorizes the
*launch*, and an irreversible external change still asks its own approver at the point it happens.

## What was measured rather than assumed

- The loop end to end through the real tools: a workflow with a `job` step composes, refuses to run
  (naming the step and the route), runs after the approval, and — driven, not asserted — stops
  running when the document is re-composed, because the stored fingerprint no longer matches.
- Both store backends lapse it identically, since a property that held in only one is a property
  this deployment does not have.
- The absence of a self-approval path, scanned over `registered_tools()` by behaviour.
- The two SQL statements share no column: `_UPSERT` is the agent's write, `_APPROVE` is the
  person's, and folding them into one is the tempting simplification this asserts against.
