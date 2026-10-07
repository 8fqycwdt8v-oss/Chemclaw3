# D-2026-10-07-the-architecture-programme — Postgres holds the knowledge and the coordination, deepagents stays, tenancy is RLS, and the record gets lean

**Status:** accepted · **Date:** 2026-10-07

## Context

The 2026-10-07 architecture review read all three repositories and found the core layering sound,
with debt in four places: the knowledge graph's git store (≈3 notes/s cluster-wide behind one
advisory lock, a NetworkX copy per pod, a clone per pod), per-process limits and a singleton
worker, an agent layer that works around `create_deep_agent` in ~30 places, and a documentation
record that outweighs the code (55.6% of `src/` lines are prose, 761 ADRs, 186k lines of Markdown).
`tasks/todo.md` plans the programme in waves W0–W8; this record takes the choices it depends on.
The baseline is `docs/planning/architecture-baseline-2026-10-07.json`.

## Options and decisions

**1. Where the knowledge graph lives.** Options: keep git and batch writes (`BatchingNoteWriter`,
8.5–31.6 ms/note); Postgres with git as an export mirror; Postgres only; a graph database.
**Decision: Postgres only.** Git leaves the knowledge path entirely — no mirror. History is a
revisions table, human edits go through the API, an export command produces Markdown on demand.
A graph database is declined: one more store to back up, secure and tenant-isolate for traversals a
recursive CTE answers at this corpus size.
Supersedes the storage half of `D-004-knowledge-as-a-markdown-git-graph-networkx-not-a` once W4
ships; the note *format* (frontmatter + body) is unchanged.

**2. Shared coordination.** Options: Postgres only (advisory locks, `SKIP LOCKED`, counters,
`LISTEN/NOTIFY`); Postgres plus Redis. **Decision: Postgres only.** It is already required, pooled
and backed up, and the rates involved are turns per second.

**3. The agent builder.** Options: keep `create_deep_agent` and its workarounds; a first-party
builder over `create_agent`; keep `create_deep_agent` and move every workaround onto a public seam
or upstream. **Decision: the third.** An off-the-shelf builder keeps upstream improvements arriving
as lockfile bumps; a first-party builder would make every one of them a port. The standard is that
deepagents must cover everything a self-built builder could: each workaround is migrated to a public
extension point, contributed upstream with a red-when-fixed test, or argued as the remaining gap.
The first-party builder is declined.

**4. Tenancy.** Options: one deployment per tenant; `tenant_id` + Postgres row-level security in one
deployment; schema per tenant. **Decision: `tenant_id` + RLS.** Deployment-per-tenant is declined
as the model (it multiplies every operational cost by the tenant count), and schema-per-tenant is
declined (migrations × tenants, and the pool cannot share connections across schemas cheaply).

**5. The record itself.** Options: keep the present rules (an ADR per finding, history in
docstrings, prose-policing tests); cut to rules plus a decisions index. **Decision: cut.** An ADR is
written only for a choice between options or a decline; a defect is a commit and a test. Docstrings
state what and why, not history. `CLAUDE.md` states current rules only. Tests that police prose are
retired; tests that police architecture (layering, upstream surface, contracts) stay.
`docs/decisions/CURRENT.md` is the one-page index of what is in force. Merged ADRs stay as the
archive; marking one superseded with a `Superseded-by:` line is the one edit this record permits.

**Revisit when:**

- Knowledge store: a traversal the store must answer exceeds what a recursive CTE serves within the
  turn's latency budget — measured on `kg_edges` at the corpus size of the day.
- Coordination: the shared limiter needs more than ~1k decisions/s, measured from
  `chemclaw_*` limiter metrics.
- Builder: a deepagents release removes or breaks a public seam this system relies on and upstream
  declines to restore it — the `tests/test_upstream_surface.py` lane turning red twice for the same
  seam is the signal.
- Tenancy: a tenant requires separate encryption keys or a separate database by contract.
- Record: a decision is re-litigated because its reasoning was cut — a review comment citing a
  missing rationale is the signal.
