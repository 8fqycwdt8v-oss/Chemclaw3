# D-2026-10-21-a-data-source-contributes-its-own-surface — a data source may ship read-only tools, skills and profiles, declared beside its halves

**Status:** proposed · **Date:** 2026-10-21

## Context

A data source (`ingest/sources/<name>/datasource.yaml`, D-120) contributes up to three halves:
`ingest` (an `ElnAdapter`), `retrieve` (a `SourceRetriever`) and `commitments`. It cannot
contribute an agent tool, a skill or a profile: `DataSourceManifest` has no such field, and
`tests/test_layering.py` has no `ingest → agent` edge. A connector bundle can ship all three
(D-118). So what a site knows about a corpus lives in YAML comments the model never reads. For
example, "Pistachio is patent precedent with partial atom-mapping" or "yields before 2022 are
crude". And any question beyond the one free-text sweep has no tool to answer it.

## Options

1. **Status quo.** Sources stay corpus-only; source-specific judgment goes into global skills that
   name the source by string. Costs nothing; the judgment stays detached from the thing it is
   about and is not switched off with it.
2. **A `surface:` block on `datasource.yaml`.** It lists `module:callable` tool references, a
   `skills/` and a `profiles/` directory, with one `SurfaceSpec` model shared with the connector
   manifest. Sources stay in-process and read-only. Costs one model, one loader in `agent/` and
   validator work.
3. **Merge sources into connectors.** One manifest kind with corpus halves. It removes a seam but
   erases the distinction D-120 draws: a source runs in the process that needs it, a connector in
   its own process. It also moves every driver into a separately deployed server.
4. **Source tools as fleet servers.** `Chemclaw3-mcp` forbids request-time egress, so a fleet
   server cannot reach a site's ELN or lakehouse. The fleet's rules would have to change for every
   source.

## Decision

**Option 2.** A source may declare `surface: {tools, skills, profiles, bind, summary}` using the
`SurfaceSpec` model the connector manifest also adopts. Every source tool must be `read_only`. That
is now a validator, enforcing the rule `ingest/sources/README.md` already states: a source never
acquires a write path. Tools are registered from `agent/source_tools.py`, so the layering stays
`agent → ingest`. Skills and profiles are discovered beside the connectors' and labelled
`source:<name>`.

## Consequences

- `DataSourceManifest` gains `surface:`. `cli/validate_datasources.py` resolves and binds each
  tool reference. `tests/test_authz.py` keeps failing on any unclassified tool.
- `source_tool_names()` feeds `authz.read_only_tool_names()`. A name collision with core or a
  connector is a startup error naming both.
- The operator knowledge in the shipped manifests' comments moves into `SKILL.md` files.
- Held by an extension of `tests/test_datasource_seam.py`: a fixture folder with a surface yields a
  working tool and a discovered skill with no core edit.
- Plan item: W10 Track A of `docs/planning/sources-and-compute-programme.md`.
