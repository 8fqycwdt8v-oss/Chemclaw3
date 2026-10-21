# D-2026-10-21-entitlement-is-declared-on-the-source-manifest — who may read a source is an `access:` block the registry enforces, not a check each half writes

**Status:** proposed · **Date:** 2026-10-21

## Context

Only one source checks the caller. `ShareDocumentRetriever._entitled()` compares
`get_current_roles()` with its binding's `required_roles` and raises `RetrieverSkip` when they do
not match. Every other half returns rows to anyone the turn serves.

Two plans would widen the gap:
- source tools (D-2026-10-21-a-data-source-contributes-its-own-surface);
- typed queries (D-2026-10-21-a-source-is-queried-through-typed-generated-tools).

Under them, each new tool would have to remember to check, and a forgotten check leaks quietly.

## Options

1. **Each half and tool checks for itself** (status quo). One more place to forget, per tool.
2. **An `access:` block on `datasource.yaml`**, either `public: true` or `roles: [...]`, the shape
   the share binding already uses. The registry filters the active sources, and the source's tools
   are withheld per turn for an actor without the role.
3. **Rely on row-level security alone.** RLS scopes tenants, not per-source entitlements, and does
   nothing for a warehouse this system does not own.

## Decision

**Option 2.**
- `active_retrieve_sources()` and `source_tool_names()` filter by the turn's roles.
- An unentitled actor gets neither chunks nor tools. The tool is **withheld**, not refused, so its
  schema costs nothing and its existence is not disclosed.
- The share half keeps its own check only on `read_document`, which is reached by note id rather
  than through the registry.

## Consequences

- For one release, a manifest without `access:` behaves as today: the share keeps its binding's
  roles and the others stay public. After that, `access:` is required.
- Held by a leak test over every shipped source and tool for an actor with no roles, which
  asserts that no chunk, row or schema reaches it.
- Plan item: W10 Track D.
