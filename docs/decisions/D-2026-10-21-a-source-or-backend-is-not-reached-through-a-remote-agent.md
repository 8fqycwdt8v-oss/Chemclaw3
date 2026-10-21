# D-2026-10-21-a-source-or-backend-is-not-reached-through-a-remote-agent — data sources and compute backends are integrated as governed tools and durable jobs, not as agents behind MCP

**Status:** proposed · **Date:** 2026-10-21

## Context

A common proposal for scaling an agent across many data systems is one specialised agent per
source or per compute backend, each reached by the coordinating agent over MCP. This system
already measured the nearer form of that idea. In D-2026-09-27, in-process helpers were used in
6 of 48 permitted runs and peer handoffs in 0 of 24. Delegating cost a median 9–30 % more tokens
with no quality gain beyond run-to-run variation.

Three of the system's invariants would also not survive a remote agent:
- **Governance.** Audit, authorization and the plan gate apply only to graphs compiled by
  `build_langgraph_agent`.
- **Citations.** Every evidence chunk must cite a note or source.
- **Durability.** Durability lives only in Temporal.

## Options

1. **Remote agent per source or backend over MCP.**
   - It scales teams and context windows.
   - It adds a second model hop per question.
   - A citation becomes a prompt property.
   - The agent hides durability inside another process.
   - Per-user entitlement must be re-implemented in each agent, because the fleet logs and never
     trusts `X-Chemclaw-Actor`.
2. **Governed tools and durable jobs.**
   - Sources get typed query tools and skills
     (D-2026-10-21-a-data-source-contributes-its-own-surface,
     D-2026-10-21-a-source-is-queried-through-typed-generated-tools).
   - Backends get driver manifests and one external-job workflow
     (D-2026-10-21-long-running-compute-runs-on-a-backend-seam).
   - Isolation, where a source needs exploration over several steps, comes from an in-process
     `task` helper on a per-source profile.

## Decision

**Option 2.** Remote per-source and per-backend agents are declined as the integration pattern.

## Consequences

- A source owned by another team is still attached as a source or connector with a bearer
  credential (D-2026-08-09-a-connector-we-do-not-run), not as an agent.
- The delegation measurement is re-run on the new probe sets under W14.6 before any roster default
  changes.

Revisit when: a source must be reached that is owned by another team, and that team ships an agent
endpoint that returns `EvidenceChunk`-shaped results with source citations and accepts a verified
actor identity, which the fleet's identity contract does not provide today.
