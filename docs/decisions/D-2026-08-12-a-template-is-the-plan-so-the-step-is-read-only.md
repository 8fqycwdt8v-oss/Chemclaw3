# D-2026-08-12-a-template-is-the-plan-so-the-step-is-read-only — A template is the pre-approved plan, so its agent step is ungated and read-only by default

**Status:** accepted · **Date:** 2026-08-12

Settles what [`D-168`](D-168-a-template-step-runs-as-its-requester.md) left open for the one
step kind it could not simply govern like a chat turn, and narrows the surface
[`D-167`](D-167-an-approval-authorizes-a-request-not-a-session.md)'s gate would otherwise have bounded.

## Context

A template's `agent` step runs a real model turn inside a fixed procedure. Every other step kind is
pinned by the file — a `tool` step names its tool, a `job` step names its job — but this one exists
precisely so the reasoning inside it is *not* pinned, and a model that can reason can also decide to
call something.

`run_agent_step` runs with `harness_enabled=False` (D-168), which means the plan gate is not
attached: `gate_applies` requires the harness. That was recorded as a consequence of not having a
session rather than as a decision, and it left the question unanswered — is an `agent` step allowed
to write?

Two facts decide it, and they point in opposite directions:

- **A template *is* the pre-approved plan.** It is a human-authored, git-committed, reviewed YAML
  file, and nothing at run time can create one. The plan gate exists to put a human between an
  autonomously-chosen plan and its execution; that human is the template's author. Gating the step
  would ask for approval of a plan nobody wrote, in a session that does not exist — and would refuse
  every write inside every template forever, which is not a posture, it is an outage.
- **But the *step* is not the plan.** The file pins the sequence; it says nothing about what the
  model reaches for inside step three. What the gate also bought was a bound on that, and dropping
  the gate drops the bound with it.

## Decision

**Template agent steps stay ungated by the plan gate. An agent step gets a read-only tool surface by
default, and may reach only the write tools the template explicitly declares.**

```yaml
  - id: record
    kind: agent
    write_tools: [propose_knowledge_note]
    prompt: Write up what step two found and propose it as a note.
```

### 1. The narrowing is structural, not a filter

`durable/template_activities.step_profile` sets the profile's `tool_names` to
`advertised − (side_effecting − declared)`, and the graph is built from that. A compiled graph's
`ToolNode` is built from the list it is given, so a tool absent at construction cannot be called at
all — this repo has twice rejected filtering an advertised list while leaving the capability
reachable, and this is not a third time.

The profile's existing `tool_names` dial carries it rather than a new mechanism: it is the documented
attenuation seam, it spans **both** halves of the surface (`_capability_tools` for the in-process
tools, `connector_specs` for each bundle's allow-list), and it re-narrows the skills backend for
free.

### 2. The profile is resolved once

`run_agent_step` used to resolve it twice — the raw name to `connector_specs`, a modified copy to
the builder. Narrowing only the builder's copy passes every in-process assertion and leaves the
entire connector surface bound, including `compute_xtb_energy`, which `connectors/calc` classifies
`state_changing` and which `authz.side_effecting_tools`'s own docstring names as the tool an
in-process-only set would have missed. Measured on the mutation: with the two calls split again, the step opened connectors still
advertising the write, and the stand-in tool's body ran.

### 3. The classification is the one that already exists

`agent.authz.side_effecting_tools()` — in-process writes ∪ every enabled connector's declared
`state_changing` and jobs ∪ every template launcher — the same set `refuse_writes_on_dry_run` and
`enforce_plan_approval` decide with, and the one `tests/test_authz.py` holds to a partition of the
live registry. A second list would be a second source of truth, and it would be wrong in the same
direction each time: only a bundle's manifest knows that `compute_xtb_energy` spends compute while
`resolve_compound` is a lookup.

### 4. A refusal, for the wording — not for the enforcement

`UndeclaredWriteRefusal` (`agent/tool_authz.py`) is attached inside `audit` and before
`enforce_tool_authz`, only when a profile narrows at all. It stops nothing that structure has not
already stopped. What it changes is what everyone reads, measured by deleting it and running the same
turn:

| | model gets | audit `detail` |
|---|---|---|
| without | `ToolMessage(status="error")` "…is not a valid tool, try one of [list_attachments, read_attachment, …]" | the same sentence |
| with | `"Refused: compute_xtb_energy changes stored data or starts work, and this agent was not given it…"` | the refusal |

`status="error"` reaches Anthropic as `is_error`, the retry-inviting signal `_refusal_message` exists
to avoid; the text invites the retry in words too, for a tool withheld on purpose rather than
mistyped; and it writes the agent's whole remaining inventory into the field an auditor reads as
*what happened*. The row itself is **not** what this buys, and an earlier draft said it was:
`ToolNode` returns the invalid-name message from inside the wrapper chain, so `returned_failure`
books an `outcome="error"` row either way. Intercepting an unbound name is possible because
`ToolNode` defers name validation "to allow interceptors to short-circuit requests for unregistered
tools" — its words, at `langgraph/prebuilt/tool_node.py`.

### 5. The declaration is validated, because a subtraction is silent

`make template-validate` rejects a declared name that does not exist, one that is not in
`side_effecting_tools()`, and one the step's profile does not advertise. The middle check is the load
bearing one: a read tool is reachable with no declaration, so naming one grants nothing — and
accepting it would let the list drift into a general allow-list wearing a write-list's name, which is
how a narrowing gets widened by people writing what looks like documentation.

## Consequences

- The shipped `hazard-briefing` template is unchanged: its `brief` step calls nothing, so it takes
  the read-only default, and its resolved surface is 30 tools disjoint from every write.
- A template that wants a write now says so in the file, which is the property that makes the
  capability reviewable in the same pull request as the procedure.
- `write_tools` crosses the activity boundary in `AgentStepInput` and is taken from the *pinned*
  template, so editing a file cannot widen a run already in flight — the same rule pinning the
  definition already gives the sequence.
- Declaring a write restores it and authorizes nothing: `enforce_tool_authz`, `authorize_trigger`
  and the dry-run guard all still apply to it, against the run's requester.
- `UndeclaredWriteRefusal` is registered in `durable/publish._BAD_DATA_TYPES`, because an
  `AuthorizationError` subclass that could cross an activity boundary must fail fast rather than
  retry a decision that will never change.
