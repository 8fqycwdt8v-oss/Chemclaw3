# D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor — A specialist inherits the caller's authority, narrowed

**Status:** accepted · **Date:** 2026-08-10

Companion to
[`D-2026-08-10-langgraph-rebuild-of-the-conversation-layer`](D-2026-08-10-langgraph-rebuild-of-the-conversation-layer.md),
which decides the rebuild. This one decides what a subagent is allowed to be.

## Context

The rebuild adds a supervisor plus specialist subagents — one per capability cluster across the
seven connector bundles (`evidence`, `computation`, `design`, `safety`, `reporting`) — and
context-isolating ephemeral delegation via deepagents' `task` tool.

The substrate is already here and already has the right shape. `AgentProfile`
(`agent/profiles.py`) is an **attenuate-only** bundle of `tool_names` + `mcp_server_names` +
instructions, discovered from `data/profiles/*.yaml` and connector bundles, with
`_reject_unknown_tool_names` failing the *build* when a profile names a tool nothing provides. Its
own file says it: *"Everything here only ever narrows: the audit trail, the per-tool authorization
gate and the skill role gates all run after this, so a profile cannot widen what its caller may
do — it can only give them a smaller, sharper agent."*

A specialist is therefore a profile plus a compiled subgraph, and not a new concept. The risk is not
that this is hard; it is that a second agent is the obvious place for an authority check to go
missing, and this system has already been bitten by exactly that.

**The precedent.** D-040 shipped a documented GxP gate — in `plan_only` the agent proposes and waits
for a human — that did not exist. MAF's `AgentModeProvider.before_run` injected a `mode_set` tool
declared `approval_mode="never_require"` and instructed the model to call it when *it judged* that
approval had been granted. Because the audit middleware attributes every tool call to the ambient
actor, the trail recorded the agent's self-authorization under the **chemist's** Entra oid. As
`agent/harness_mode.py` puts it: that is worse than an unrecorded flip — it is an
attributable-looking approval with no human act behind it.

A subagent is the same hazard with a longer lever. It runs tools, and if the actor does not travel
with it, every call it makes is either unattributed or attributed to the wrong person.

## Decision

Four invariants. Each is a test, not a convention.

1. **A subagent's surface is an attenuation of its caller's, never a widening.** Specialists are
   built through `AgentProfile` and `chemclaw_agent._narrow`; a handoff that would add a tool the
   caller does not hold is a build-time error, exactly as an unknown tool name already is. Both
   dials attenuate — `mcp_server_names` selects whole connectors, `tool_names` narrows each
   surviving connector's allow-list.

2. **`require_actor` reject-if-absent holds inside every subagent.** This is F4's core rule and it
   does not get a carve-out for a nested graph. It needs asserting rather than assuming:
   `SubAgentMiddleware` filters parent state through `_EXCLUDED_STATE_KEYS` to keep contexts
   isolated, and deepagents issue #569 asks whether `runtime.config` reaches a subagent invocation
   at all. If identity does not propagate, the actor is carried explicitly in the handoff payload.
   Verified before the team is built, not after.

3. **The audit middleware wraps subagent tool calls, recording the specialist beside the human.**
   The trail names two things — which person authorized the turn and which agent made the call — and
   loses neither. Attribution to "the agent" is what makes a GxP trail worthless, and attribution of
   an agent's act to a person is the D-040 failure repeated.

4. **Skills do not inherit.** deepagents custom subagents get no skills unless they declare them,
   which is the correct default here rather than an inconvenience: it composes with
   `skill_role_gates` and `ToolScopedSkillsSource`, whose whole job is that judgment about a tool
   the agent cannot call reads to the model as an available path. Each specialist declares its own.

**`safety` is not attenuable away.** The safety connector is a gate, not a capability. A profile may
narrow which computation a specialist can do; it may not produce a specialist that screens nothing.

## Consequences

- A specialist is configuration (`data/profiles/*.yaml` plus a connector bundle's own), not code.
  Adding one is a manifest, which is the same property D-118 gives connectors and D-120 gives data
  sources.
- The supervisor pattern is chosen over a swarm for one reason that outranks routing latency: one
  routing node means every delegation decision is visible in the trace, and here the trace *is* the
  regulated record.
- Team quality is not a code property. A supervisor that mis-routes is worse than the single agent
  it replaced, so hand-off accuracy and per-specialist token cost are measured against the
  single-agent baseline during live re-validation, and the team ships disabled by default behind a
  profile flag until that measurement exists.
