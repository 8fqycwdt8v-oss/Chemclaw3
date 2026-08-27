# `chemclaw.agent` — the conversation layer

**One engine: LangGraph.** `langgraph_agent.build_langgraph_agent` compiles the agent
over `deepagents.create_deep_agent` — one graph per turn, because LangGraph binds tools at
construction and a connector's MCP session belongs to exactly one turn (measured at ~60 ms,
cheaper than the process-lived agent it replaced). `create_deep_agent` assembles a stack of
its own and splices this repository's into it **by `.name`**, so the compiled order is not
the order `_middleware` lists; `tests/test_middleware_order.py` pins what actually compiles,
and one entry — `FilesystemMiddleware` — deliberately shares upstream's name so it takes its
place and withholds the shell and the delete verb. Turn state is a declared schema
(`state.py`) persisted by a Postgres checkpointer (`checkpointer.py`); the plan is
`TodoListMiddleware`'s todo list; every tool call crosses the chain
`langgraph_agent.tool_call_middleware` builds, whose order is load-bearing and documented
there. The `task` tool is not optional — upstream refuses to let a profile strip the
middleware that registers it — so `subagents.py` supplies the one helper it reaches, compiled
through this same builder rather than inherited ungoverned. The Microsoft Agent Framework this layer
was first built on is gone
(`docs/decisions/D-2026-08-10-langgraph-rebuild-of-the-conversation-layer.md`) — replaced
for defect load rather than capability. `api/events.py` was the conformance boundary the two
engines were scored against — additive changes only, and only after the comparison was made
(M9/M10 add `HandoffEvent`, `EvidenceSourceEvent` and an `agent` field).

**Responsibility:** conversation orchestration and short reasoning steps. Agents
advertise tools, load Skills on demand, and
kick off durable work — but they hold **no durability for that work** (that is
Temporal's job; the checkpointer under the graph holds this turn's state and nothing
more) and **no domain judgment** (that lives in `skills/`).

An agent tool that starts a long job returns immediately with a `job_id`; the
work runs as a Temporal workflow (see `durable/README.md`, and each bundle's own `workflows.py`).
See `docs/reference/architektur.md` §1 and CLAUDE.md's four-layer rule.

**Current tools:** knowledge-graph read + PR-gated write (`graph_tools`), cross-source
evidence (`research_tools`), condensing many whole protocols into one comparison
(`protocol_tools`, over `agent/condense.py`), confirmed-answer capture (`memory_tools`), and the durable
report launcher plus the one status tool every durable job is collected with
(`durable_tools`). Calculators and optimization campaigns are the `calc` bundle and the `bo` bundle
now, advertised out of `connectors/` — including their durable launchers, which are generated from
each bundle's manifest rather than hand-written here (D-118). There is no QM/DFT job: the whole
HPC/DFT tier was deleted by `D-2026-08-26-semiempirical-is-the-whole-tier`, and this sentence still
advertised it three weeks later, which is why `tests/test_repo_map.py` now resolves every bundle
name these documents write. Structural fingerprint search is reached over the MCP capability
servers, and **only** over them: the
in-process `search_tools` wrapper that shadowed them is gone, along with the "keep the two in
sync" obligation it had already broken (D-2026-08-05). Every tool call is recorded by the one audit
middleware (`audit`), and retrieved note content is framed as data before it reaches the
model (`framing`).

**Skill visibility (`skill_access`)** is three narrowings over one discovered set, none of which can
widen it: what the deployment enabled (`skills_enabled`), what this agent can actually *do*, and who
the caller is (`skill_role_gates`, the Phase-6 seam). The middle one reads each skill's declared
`tools:` and drops a skill whose whole declared capability is absent from the advertised surface —
judgment about a tool the agent cannot call reads to the model as an available path, which is the
same defect `cli/validate_prose_contract` exists to catch, arriving through the skill rather than
through the prompt. The predicate is enforced on the **backend** (`skill_backend.py`), not on the
advertised list: `deepagents.SkillsMiddleware` publishes each skill's *path* into the system prompt
and expects the model to fetch the body with a file-read tool, so a listing-only filter would hide
a role-gated skill and then hand it to anyone who guessed the path the prompt has already taught.

**What is deliberately not here any more.** The turn's ambient primitives — its identity, its
session id, the tool registry and the signal side-channel — are `chemclaw.core`
(`core/identity_context.py`, `core/session_context.py`, `core/tool_registry.py`,
`core/turn_signals.py`). Each is a `contextvar` or a dict over plain values with no first-party
imports, and filing them under this package is what made `kg`, `connectors` and `templates` import
orchestration in order to stamp an actor or declare a tool. The counterpart that *did* live here —
a contextvar carrying the turn's live framework session object, because the plan and the
awaiting-job bookkeeping hung off that object rather than off the id — went with the framework:
both are declared fields of `state.py` now, so the gate reads the plan from the state the graph is
running and there is no second place for it to be.
