# `chemclaw.agent` — MAF conversation layer

**Responsibility:** conversation orchestration and short reasoning steps, built on
the Microsoft Agent Framework. Agents advertise tools, load Skills on demand, and
kick off durable work — but they hold **no durability themselves** (that is
Temporal's job) and **no domain judgment** (that lives in `skills/`).

An agent tool that starts a long job returns immediately with a `job_id`; the
work runs as a Temporal workflow (see `workflows/`). See `docs/reference/architektur.md` §1
and CLAUDE.md's four-layer rule.

**Current tools:** knowledge-graph read + PR-gated write (`graph_tools`), cross-source
evidence (`research_tools`), confirmed-answer capture (`memory_tools`), and the durable
report launcher plus the one status tool every durable job is collected with
(`durable_tools`). Calculators, optimization campaigns and the QM/DFT job are connector
bundles now, advertised out of `connectors/` — including their durable launchers, which are
generated from each bundle's manifest rather than hand-written here (D-118). Structural
fingerprint search is reached over the MCP capability servers, and **only** over them: the
in-process `search_tools` wrapper that shadowed them is gone, along with the "keep the two in
sync" obligation it had already broken (D-2026-08-05). Every tool call is recorded by the one GxP audit
middleware (`audit`), and retrieved note content is framed as data before it reaches the
model (`framing`). The interaction-approval starter/decider seam lives in
`interaction_tools`.

**Skill visibility (`skill_access`)** is three narrowings over one discovered set, none of which can
widen it: what the deployment enabled (`skills_enabled`), what this agent can actually *do*, and who
the caller is (`skill_role_gates`, the Phase-6 seam). The middle one reads each skill's declared
`tools:` and drops a skill whose whole declared capability is absent from the advertised surface —
judgment about a tool the agent cannot call reads to the model as an available path, which is the
same defect `cli/validate_prose_contract` exists to catch, arriving through the skill rather than
through the prompt.

**What is deliberately not here any more.** The turn's ambient primitives — its identity, its
session id, the tool registry and the signal side-channel — are `chemclaw.core`
(`core/identity_context.py`, `core/session_context.py`, `core/tool_registry.py`,
`core/turn_signals.py`). Each is a `contextvar` or a dict over plain values with no first-party
imports, and filing them under this package is what made `kg`, `connectors` and `templates` import
orchestration in order to stamp an actor or declare a tool. What stayed is the half that genuinely
needs MAF: `live_session.py` carries the live `AgentSession` *object* for the plan gate and the
harness todo list, because that state hangs off the object and not off the id.
