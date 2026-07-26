# `agents/` — MAF conversation layer

**Responsibility:** conversation orchestration and short reasoning steps, built on
the Microsoft Agent Framework. Agents advertise tools, load Skills on demand, and
kick off durable work — but they hold **no durability themselves** (that is
Temporal's job) and **no domain judgment** (that lives in `skills/`).

An agent tool that starts a long job returns immediately with a `job_id`; the
work runs as a Temporal workflow (see `workflows/`). See `docs/architektur.md` §1
and CLAUDE.md's four-layer rule.

**Current tools:** fast calculators (`calc_tools`), Bayesian-optimization proposals
(`bo_tools`), knowledge-graph read + PR-gated write (`graph_tools`), cross-source
evidence (`research_tools`), confirmed-answer capture (`memory_tools`), and the durable
QM job adapter (`qm_tools`). Structural fingerprint search is reached over the MCP
capability servers, not in-process. Every tool call is recorded by the one GxP audit
middleware (`audit`), and retrieved note content is framed as data before it reaches the
model (`framing`). The interaction-approval starter/decider seam lives in
`interaction_tools`; role-scoped skill visibility in `skill_access` (a Phase-6 seam).
