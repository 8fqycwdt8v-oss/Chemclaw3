# D-2026-09-15-an-agent-authored-workflow-is-read-only-by-construction — the agent composes a procedure, and cannot compose a write

**Status:** accepted · **Date:** 2026-09-15 · Holds
`D-2026-08-12-a-template-is-the-plan-so-the-step-is-read-only` by restoring its premise rather than
by re-arguing it. Beside
`D-2026-09-15-a-bound-that-stops-at-the-seam-is-not-a-bound` (same seam, same day).

## Context

This system could run a fixed multi-step procedure and could not *make* one. A template was a
git-committed YAML file, so a chemist whose weekly question is three tools in a fixed order paid
three model calls and three round-trips for it every week, and the order lived in whatever the
model remembered. Asking for a template meant asking for a pull request.

**The reason it worked that way is load-bearing and is why this is not a small feature.**
`D-2026-08-12` exempts a template's `agent` step from the plan gate, and argues the exemption from
a premise: the file *"is authored by a person, committed to git and reviewed"*, and **"nothing at
run time can produce one"**. A tool that produces one at run time falsifies that sentence. The
exposure is not theoretical and it is not only `write_tools:` — a `tool` step naming
`record_knowledge_note` runs through `invoke_governed` under the requester's identity, so
`enforce_tool_authz` still decides, while `enforce_plan_approval` returns early on
*"No session means no plan to approve"*. That early return is correct for a reviewed procedure and
is a hole for one the agent wrote a moment ago.

## Decision

**An agent-authored workflow may name no side-effecting tool, no durable job and no `write_tools`.**

`templates/composed.authored_problems` is the whole rule, in one function, asked by the write path
and by the run path. The exemption is about writes; a document that cannot contain one never
reaches the question. So nothing about the plan gate changes, and a hand-written template in
`data/templates/` keeps every capability it had, including its `write_tools`.

**Re-attaching the plan gate was the obvious alternative and is not a control.** A template step
runs in an activity with no session, so `enforce_plan_approval` would refuse every write in it for
want of a plan nobody can approve — which is the same "no writes" with more moving parts and a
worse failure mode.

**Checked twice, and not for its own sake.** The two checks happen at different times against
possibly different deployments, and `side_effecting_tools()` *grows*: a bundle enabled between
composing and running turns a name that was a read into a write, and only the run-time check, with
the run's own set, notices.

**The same `Template` model, not a parallel one.** A composed document is validated, stored, and
run as the shape a `data/templates/*.yaml` file parses into — so unique step ids, well-formed
`${…}` spans and the forward-reference refusal apply for free, `TemplateWorkflow` runs it without
knowing where it came from, and a composed workflow gets wave scheduling and resume because those
belong to the run rather than to either launcher. `registry.start_template_run` is extracted for
the same reason: the deterministic id, the run ceiling, the idempotent rejoin and the `JobSignal`
are properties of a template run, and a second copy is how one launcher quietly loses one.

**Two tools, not three.** A listing rides on the refusal `run_composed_workflow` gives an unknown
name. A tool schema is re-sent on every model call; a listing is needed on the turn a name is
already wrong.

**Keyed `(owner, name)`.** A composed workflow is one chemist's working procedure, not a
deployment's catalogue. Resolving against the caller's own rows is what stops a name reaching
somebody else's steps — a worse failure than a collision, because nothing about the result would
look wrong.

## What it costs, stated rather than discovered

**A composed workflow cannot run a calculation.** Every durable job launcher is state-changing by
classification, so the expensive computation these procedures exist to sequence is exactly what an
*agent-authored* one may not contain. It composes reads — enumerate, look up, retrieve, screen —
and then reasons over them in an `agent` step whose own surface is already narrowed to reads. A
procedure that needs a ranking is a template a person writes, which is the same answer `skills/`
gives for judgment that must be reviewed.

**The prefix.** `compose_workflow` and `run_composed_workflow` measure **978 tokens** together —
752 and 226 — taking `default` from 67,166 to 68,144 against a ceiling of 67,200, i.e. **34 tokens
of headroom were left and this needed 944 more**. `CEILINGS["__default__"]` goes to 69,000. By the
rule that file already states, `agent_tool_result_clear_trigger` rises 1,800 and costs nothing,
while `agent_context_token_budget` is derived downwards from the window and cannot follow — so the
thread allowance falls 40,500 → 38,700, **4.4% of the thread**. That is the price, paid where the
constraint is.

The split between the two tools is the argument for both being declared: `run_composed_workflow` is
226 because it takes a name and a dict, while `compose_workflow` publishes the step and input
models a workflow is *written* in and cannot be smaller without the model guessing the shape.

## What was measured rather than assumed

- The rule, in both directions, on every refusal: a workflow of reads is allowed; a `tool` step
  naming `record_knowledge_note` is refused; a `job` step is refused; a `write_tools:` line is
  refused; two problems report as two rather than one; and widening `side_effecting_tools()`
  between composing and running flips a stored workflow from runnable to refused.
- Both store backends prove the same claims, including that one owner's names are not another's.
- The prefix figures above, in one process through the same conversion path production uses.

## What stays open

Discovery across sessions is one refusal deep: a chemist who has forgotten a name learns the real
ones by guessing one. A third tool would answer it and costs prefix on every model call to answer a
question asked once; the cheaper shape, if this turns out to matter, is the session's own opening
context rather than a schema. `docs/planning/BACKLOG.md` carries it.
