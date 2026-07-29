# D-103 — X8: the calculators as an MCP server, and the line identity draws

**Context.** The heavy half of this system's dependency closure — RDKit, tblite, scipy, and the
`xtb`/`crest` binaries — belongs to the calculators, as does the CPU load. Hosting them in the
agent's process means an optimization that saturates a core competes with a conversation for it.
The requirement was stated plainly: run them in their own pod.

**Decision.** `mcp_servers/calc` (`mcp-calc`), a third FastMCP capability server alongside
`molfp`/`rxnfp`, hosting the seven tools that compute: `compute_xtb_energy`,
`compute_electronic_properties`, `predict_site_reactivity`, `optimize_geometry`,
`compute_thermochemistry`, `predict_solubility`, `predict_pka`. Thin, like its siblings — every
body already lived in `calc/`, so this is transport. It runs as its own pod via
`CHEMCLAW_COMPONENT=mcp-calc`, or over `http` against an already-running remote.

**The tools were moved, not copied.** One capability advertised twice is a surface the model has
to choose between for no reason, and the two copies drift.

### What cannot move, and why it is not about chemistry

`compute_reaction_energy`, `compare_solvents`, `scan_coordinate` and `sample_conformers` route to
Temporal above a cost threshold; `run_xtb_task` is role-gated. All five need `require_actor()` and
`get_current_session_id()` — the turn's authenticated user and the conversation to notify, both
**ambient** and, by the F4-T3 reject-if-absent rule, never model-supplied. An MCP server is a
separate process with no conversation and no authenticated user; the only way to give it those
would be as tool *arguments*, which would make identity a model-authored value — precisely what
that rule exists to prevent.

So the boundary is **MCP carries capability, the agent keeps identity**, and it predicts what can
ever move: anything that computes, nothing that authorizes. The tools that stay are the ones that
*decide and delegate* — they price the request and either run it or hand back a job id — while the
computation itself is the same `calc/` code the server hosts.

### The one change that was not mechanical

`scripts/validate_skills` resolved a declared tool against the in-process registry only, so every
skill teaching a moved tool would have failed the gate. Widening it to include each configured
server's `allowed_tools` is not a workaround for the migration — it is the correct model: **a
skill names a capability, and which process delivers it is a deployment decision the judgment
layer should be insulated from.** The evidence that this is right is that **no skill changed** in
a migration that moved seven tools out of process. The check is not weakened: an invented tool
name still fails, and both cases are tested.

`test_mcp_transport` needed no edit either — it parametrizes over configured stdio servers, so it
picked the new one up and proved it spawns as a real subprocess advertising exactly its seven
tools, which is the boundary that keeps anything else on that server off the agent (D-029).

### A regression the migration caused, and the better mechanism it forced

Agent *profiles* attenuate the advertised surface by name, and `mcp_server_names` narrows whole
servers. So a profile that named `predict_pka` broke: the tool was no longer in-process, and
MCP attenuation was server-granular — the choice was all seven calculators or none.

That is the same mistake the skill validator would have made, one layer up: a profile is a
statement about *capabilities*, not about which process hosts them. `tool_names` now resolves
across both transports and narrows a server's `allowed_tools` to the **intersection** with what
the profile asked for, on a copy so one profile cannot narrow the surface for everyone else. A
server with nothing asked for is not attached at all. Naming one tool grants that tool, never its
server — pinned by a test, because an attenuation mechanism that silently widens is worse than
none.

**What did not move and is worth naming:** `bo/featurize.py` imports `calc.xtb_props` directly,
in-process, because it is not a tool call — the BO featurization is library use, and MCP is the
*agent's* transport, not an internal one. A second consumer of the calculators inside the same
process is not a reason to route it through a subprocess.
