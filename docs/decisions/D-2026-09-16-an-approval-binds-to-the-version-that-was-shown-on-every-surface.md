# D-2026-09-16-an-approval-binds-to-the-version-that-was-shown-on-every-surface — what four fresh-context reviews of the composed-workflow seam found

**Status:** accepted · **Date:** 2026-09-16 · Revisits
`D-2026-09-15-an-approval-is-for-one-version-of-one-workflow` and
`D-2026-09-15-an-agent-authored-workflow-is-read-only-by-construction`, whose controls hold and
whose two *surfaces* did not hold them equally.

## Context

Four fresh-context reviews were run against the composed-workflow seam with the infrastructure up,
each asked to drive rather than read. The security argument survived every attempt to break it —
the check is strictly before the launch with no TOCTOU, the fingerprint covers every field
including `write_tools`, a step cannot be smuggled past `authored_problems` by naming both a tool
and a job, `compose_workflow` and `run_composed_workflow` are themselves side-effecting so no
composed workflow can contain either, and an irreversible effect still fails closed inside an
approved job. What the reviews found instead is a set of places where the *same* property was
enforced on one path and merely described on another.

## Decisions

**1. The terminal's approval binds to a version, in two typed lines.** `/approve-workflow <name>`
approved the document as it stood, argued from *"there is no window for the document to change
between being read and being approved, because the person doing both is this terminal."* That is
how `/approve` binds to a plan and it is false here: the **agent** also acts in this terminal, under
this same owner, between the two commands. Driven — one turn re-composed `triage` between
`/workflows` and `/approve-workflow triage`, and the chemist who read `rank_species(${inputs.smiles})`
authorised `sample_conformers("something-else-entirely")`. The first line now renders the whole
procedure and hands back the fingerprint; the second requires it and refuses a stale one, which is
the 409 the HTTP route already had. Typing the hash *is* the act of having read what it names.

**2. The approver is the owner, and that is now unrepresentable otherwise.** `ComposedStore.approve`
took a fourth `approver` argument that both callers satisfied by passing the owner twice, because
both resolve the workflow against the caller's own rows. Keeping it meant the store permitted a
state the write path cannot produce — and `agent/leaver.py`, whose erase predicate is
`WHERE owner = ANY(...)`, then had a person-naming column it could not reach while
`test_every_actor_bearing_column_in_the_schema_is_accounted_for` stayed green on a table-level
match. That is the `note_proposals.decided_by` defect reappearing through a shortcut. Removing the
parameter closes it by construction, and the erase comment now says so instead of the position
being taken by omission.

**3. A save carries an approval forward and can never grant one — in both backends.** They
disagreed. `_UPSERT` names no approval column, so Postgres kept a stored approval across a
re-compose; `InMemoryComposedStore.save` replaced the whole object, so memory destroyed it *and*
would have accepted an approval handed to it inside a `ComposedWorkflow`. The ADR asserted *"both
store backends lapse it identically, since a property that held in only one is a property this
deployment does not have"*, and that was the sentence falsified. Aligned on Postgres's behaviour,
for a reason rather than because it was the durable one: **an agent's write must not be able to
erase the record of a person's decision.** The approval still lapses, because `unapproved_jobs`
compares the fingerprint. What a reader must therefore never do is render `approved_by` alone, so
`GET /workflows/{name}` returns a derived `approved` beside it — the same correction
`GET /workflows` already had.

**4. Both launchers validate their inputs.** `build_template_tool.launch` validated against the
template's params model (the D-138 fix) and `run_composed_workflow` called the shared launcher with
a raw dict, so a composed run could omit a *declared required* input entirely and fail on
`${inputs.smiles}` deep inside a durable run — the wasted launch `unrunnable_reason` exists to
refuse. The validation moved into `start_template_run`, which is the function extracted so the two
launchers could not drift and from which this had drifted. It normalises a model to a mapping
first, because `_params_model` builds a new class per call and `model_validate` rejects an instance
of the other one on class identity.

**5. `authored_problems` fails closed on a step kind it does not recognise.** The chain was
`if ToolStep … elif AgentStep …` with no final branch, so a fourth kind added next year would be
silently permitted in the one function where "permitted" means "exempt from the plan gate".
`template_step_ceilings` takes the same position from the other side.

**6. A workflow can be forgotten.** `DELETE /workflows/{name}` and `/forget-workflow <name>`. With a
cap of `MAX_PER_OWNER` and no delete, the only way to make room was to re-compose over a name —
which destroys the document anyway *and* leaves a row whose name lies about its contents. Not a
tool, for no security reason at all: the agent can already replace a workflow by composing over it,
so a `forget_workflow` tool would add no reach.

**7. The cap's guard no longer reads its own page size.** `list_for` was `LIMIT MAX_PER_OWNER` and
the guard was `len(existing) >= MAX_PER_OWNER`, so "at the cap" and "over it" were the same answer.
Driven at 60 rows: the oldest workflows were invisible to the guard *and* to the "you have: […]"
listing, so re-composing a workflow the owner still had was refused with "you already have 50" while
`get` went on finding and running it. One spare row is the whole fix.

## What was left alone, deliberately

**An approval covers the call's shape, not one call's arguments.** `rank_species(${inputs.smiles})`
approved once runs for any smiles. That is the design and not a gap: the approval is for a
*procedure* a person keeps, which is the stated difference from a plan approval that authorizes one
turn. A per-run approval is a different feature and would need its own argument.

**One owner namespace with identity off.** With `entra_required=False` every front-door caller is
`dev-user`, so all of them share one owner namespace. `api/middleware._refuse_unauthenticated_exposure`
refuses to start that posture on a non-loopback bind, which is the control; it is recorded here so
that "keyed by owner" is not read as a tenancy boundary in dev.

**The "no tool can approve" assertion is a source-text scan.** `tests/test_api_workflows.py` reads
`inspect.getsource` of every registered tool for `.approve(`, `approved_fingerprint=` and
`approved_by=`, so a tool reaching the store through a helper module or a `getattr` would pass it.
The ADR that introduced it calls this asserted "by *behaviour*, not by name", which overstates what
it is. It stays as it is — the real guarantee is that no such tool was built, obtained the way
`decide_plan`'s is — and this paragraph is here so the assertion is not read as more than a
tripwire over the shape the mistake would most likely take.

**`stable_hash` is 64 bits.** A birthday bound of ~2^32 for a party authoring both documents, not
reachable at tool-call rates, and widening it would invalidate every stored `approved_fingerprint`.

## Stale prose corrected in the same commit

Each of these was a present-tense claim its own pull request falsified, which is
`D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` inside the commit that wrote it:
`workflow_tools.py`'s module docstring contradicting its own tool docstring about `job` steps;
`template_activities.py:run_agent_step`, which is where an agent step actually executes, arguing the
plan-gate exemption from *"nothing at run time can produce one"*; `templates/README.md` §"An agent
step is read-only" saying the same thing 95 lines before the section that describes composing one;
`infra/sql/100_composed_workflows.sql`'s header stating a rule migration 103 repealed;
`composed.authored_problems` citing a `BACKLOG.md` row for the side-effecting-tool question that no
commit ever wrote; `core/config/agent.py`'s clear-trigger comment naming a default of 109,000 over a
field reading 110,800; `api/routes/README.md` missing its `workflows.py` row; and probe `ws-19` in
`data/evals/probes/workspace.yaml`, which scored the shipped behaviour as a fabrication —
`99af3a9e` is "Five probes scored the correct answer as a fabrication" from this same window.
