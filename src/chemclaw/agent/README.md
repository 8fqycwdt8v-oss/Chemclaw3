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
middleware that registers it — so `subagents.py` supplies the helpers it reaches, compiled
through this same builder rather than inherited ungoverned. **A turn may also be several
agents rather than one** (`turn_graph.py`, `handoff.py`,
`docs/decisions/D-2026-09-19-a-handoff-redistributes-the-turns-authority-it-cannot-extend-it.md`):
with `CHEMCLAW_AGENT_PEER_ROSTER` set, `build_turn_agent` compiles a `StateGraph` whose nodes are
several of these graphs, and a `transfer_to_<peer>` tool moves the conversation between them with
`Command(goto=…, graph=Command.PARENT)`. A peer is not a helper — it keeps the acting tools and
answers the chemist directly — and its surface is the *root's* surface intersected with its own
profile, so a chain of any length is bounded by the agent that opened the turn. The roster is empty
by default, `build_turn_graph` returns `None`, and the single agent above is what a turn runs on. The Microsoft
Agent Framework this layer was first built on is gone
(`docs/decisions/D-2026-08-10-langgraph-rebuild-of-the-conversation-layer.md`); `api/events.py` is
the event contract both engines were held to.

**Responsibility:** conversation orchestration and short reasoning steps. Agents
advertise tools, load Skills on demand, and
kick off durable work — but they hold **no durability for that work** (that is
Temporal's job; the checkpointer under the graph holds this turn's state and nothing
more) and **no domain judgment** (that lives in `skills/`).

An agent tool that starts a long job returns immediately with a `job_id`; the
work runs as a Temporal workflow (see `durable/README.md`, and each bundle's own `workflows.py`).
See `docs/reference/architektur.md` §1 and CLAUDE.md's four-layer rule.

**Current tools:** knowledge-graph read + write (`graph_tools`), cross-source
evidence (`research_tools`), condensing many whole protocols into one comparison
(`protocol_tools`, over `agent/condense.py`), confirmed-answer capture (`memory_tools`), the durable
report launcher plus the one status tool every durable job is collected with
(`durable_tools`), and the artefact tools beside the chat (`exhibit_tools`, with the listing that
rides on every model request and the per-turn note announcing a chemist's edit in
`exhibit_notes`). Calculators and optimization campaigns are the `calc` bundle and the `bo` bundle
now, advertised out of `connectors/` — including their durable launchers, which are generated from
each bundle's manifest rather than hand-written here (D-118). There is no QM/DFT job
(`D-2026-08-26-semiempirical-is-the-whole-tier`). Structural fingerprint search is reached over the MCP capability
servers, and **only** over them: the
in-process wrapper that shadowed them is gone (D-2026-08-05). Every tool call is recorded by the one audit
middleware (`audit`), and retrieved third-party content is framed as data before it reaches the
model. That framing has two halves and one implementation (`framing`): the in-process channels —
a note body, an ELN procedure, an uploaded attachment, a job summary, a recalled statement —
frame their own spans at the tool that produces them, and **everything a connector returns is
framed by one `wrap_tool_call` middleware** (`tool_framing`), because those tools are not in this
process and there is no call site here to add a rule to. It keys on the `SERVED_BY` stamp
`connectors/transport` writes at handshake time — the same fact the audit trail reads for its
provenance column — so the split is structural: an in-process result carries no stamp and is never
framed twice, and a connector result cannot escape by being new
(`docs/decisions/D-2026-08-27-a-tool-result-crosses-a-boundary-and-must-say-so.md`).

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

## Module index

The tools a model calls, beside the ones named above: `analytical_tools` (does a result meet a
specification), `commitment_tools` (what a programme committed to), `evidence_tools` (how a piece of
work came to be), `operations_tools` (the operational read model), `pending_tools` (raise a question
and read what is outstanding), `dialogue_tools` (`ask_clarifying_question`), `protocol_design_tools`
(write a protocol), `workflow_tools` (compose and run a reusable workflow), `proposal_tools`
(`propose_skill` — a proposal, never a skill), `subscriptions` (standing queries) and
`attachments` (a file a chemist hands over). `tool_modules` imports them all, which registers them;
`tool_schema` derives one `StructuredTool` per capability function per process.

| Concern | Modules |
| --- | --- |
| What a profile advertises | `chemclaw_agent.py`, `profiles`, `profile_discovery`, `subagents`, `turn_graph`, `handoff`, `text_overlay` (replacement tool descriptions and prompt blocks for a text-evaluation candidate arm) |
| The tool-call chain | `tool_authz` over `authz` (the one authorization module), `audit` + `audit_store`, `tool_framing`, `tool_result_shape`, `tool_result_size`, `repeat_guard`, `refusal_route`, `tool_invocation` (the chain with no graph driving) |
| Plans | `plan_gate`, `plan_scope`, `plan_link`, `plan_state`, `plan_approval_store` |
| Cost and context | `compaction`, `context_budget`, `spend_cap`, `loop_cap`, `model_calls`, `turn_usage`, `turn_cost` + `turn_cost_store` |
| Sessions | `session` (the handle a turn runs against), `session_store`, `session_events`, `session_queue`, `turn_remotes` (requests to a turn another replica holds), `turn_resume` (whether a turn whose pod died can continue from its checkpoint), `session_members`, `session_fork`, `message_pairing`, `message_migration` (the one stored-shape converter), `turn_ambient` (the per-turn ambients a driver opens), `job_results` (wait in-turn for jobs this turn started) |
| Skills | `skill_access`, `skill_backend`, `skill_manifest`, `skill_store`, `skill_fingerprint`, `stored_skill_tools`, `org_skills`, `local_skills`, `behaviour_proposals`, `distiller` |
| The rest | `llm_provider` (the one chat-model import), `scratchpad` (the filesystem without `execute`/`delete`), `preferences`, `verifier`, `template_surface`, `leaver` (erase a departed person's conversational data), `store_setup` |
