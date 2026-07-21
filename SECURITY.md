# Security posture

Chemclaw is **pre-Phase 6**. Phase 6 (identity, RBAC, and hardening) — the layer that
authenticates users and authorizes actions — is **designed but not yet implemented**
(`docs/architektur.md` §7–§8; `BACKLOG.md` "Now — Phase 6"). This file states, plainly, what
that means for anyone deploying the current build. It is a deliberate, documented gap, not an
oversight.

## What is NOT enforced yet

- **No authentication.** Nothing validates an Entra ID (or any) token. The agent runs with
  whatever ambient credentials its process has.
- **No authorization.** Any caller who can invoke the agent can trigger every tool — including
  expensive/durable jobs (`submit_qm_job`, BO campaigns, and later HPC/DFT).
- **No verified identity in the audit trail.** The GxP tool-audit middleware (D-027) records an
  `actor`, but it defaults to `"unknown"` and is an *unverified label*, not a principal
  (`agents/chemclaw_agent.py`, `agents/audit.py`). The `oid`/`upn` carried on workflow inputs
  (`workflows/models.py`) is audit metadata, not an access check.
- **No note-level access control.** Skills and knowledge are not scoped per project/role;
  `agents/skill_access.py` advertises every skill by default.
- **Shared service credentials.** Temporal connects without mTLS/API-key and Postgres uses one
  DSN (`chemclaw/temporal_client.py`, `chemclaw/config.py`).

## What already limits or contains the risk

- **No network listener ships in this repo.** The agent is a library (`build_agent` →
  `agent.run`); the only entrypoints are the two Temporal workers (which dial *out*) and CLI
  scripts. Nothing is remotely reachable until someone adds a front door, so out-of-the-box
  exposure requires the ability to execute code in the environment.
- **The PR-gate contains writes.** Every agent-authored note lands on a branch/PR and needs a
  human merge (D-005), so an unauthenticated agent can *propose* knowledge but cannot make it
  validated "truth" unilaterally.

## Deployment guidance (until Phase 6 lands)

- **Safe:** single-tenant / trusted-network / dev use, where every caller is already trusted to
  run the whole system.
- **Do NOT** expose the agent to multiple or untrusted users (e.g. behind Copilot Studio /
  Teams / an HTTP endpoint) before Phase 6. Without authorization that lets anyone reachable
  spend compute (cost / resource DoS), read across projects, and act with no accountable
  identity in the audit trail — the last of which directly undermines the GxP goal that
  motivates the design.
- **The autonomous harness mode raises the stakes.** With `CHEMCLAW_HARNESS_ENABLED=true` and
  `CHEMCLAW_HARNESS_AUTONOMY=execute`, the agent chains tool calls (incl. durable/expensive
  jobs) without a human turn between them, bounded by `CHEMCLAW_HARNESS_MAX_LOOP_ITERATIONS`.
  It adds **no new capability** — the generic file/shell/web-search batteries are deliberately
  disabled (D-038, `docs/architektur.md` §6) — but it amplifies the impact of the missing
  authorization. Keep it `plan_only` (the default) outside trusted contexts; building the agent
  in `execute` mode emits a startup WARNING.

## What Phase 6 will add

Entra ID authentication end-to-end; authorization **centralized in the MCP server** (the one
place every tool call already carries the caller's token — so authz is not scattered across the
agent, Temporal, and Git); role-scoped skill visibility; Temporal mTLS + `oid` audit claims;
knowledge-graph ACL; and the HPC identity bridge. See `docs/architektur.md` §7–§8 and
`docs/implementation-plan.md` Phase 6.

## Reporting

This is a pre-production research system. Report security concerns to the maintainers through
the repository's private channels rather than a public issue.
