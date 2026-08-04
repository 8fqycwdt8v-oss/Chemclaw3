# D-2026-08-04-the-schema-is-a-file — A warehouse ELN's schema is a binding document, not an adapter

**Status:** accepted · **Date:** 2026-08-04 · **Builds on:** D-050 (compose two half-contracts),
D-120 (a data source becomes a manifest), D-018 (discovery is not enablement), D-089 (no external
data sources), D-005 (the PR-gate is the one write path)

## Context

The final ELN integration will be a Snowflake database carrying reaction SMILES, protocol text,
project ids, several child tables of charged amounts, yield, purity, an unenumerated number of
further tables, and a per-reaction embedding in a warehouse vector table. **Which of those are
tables, which are columns, and what any of them are called is not knowable today.**

D-120 made attaching a source free — a folder with a `datasource.yaml`, a name in
`CHEMCLAW_DATA_SOURCES`, zero core edits — and `docs/planning/DEFERRED.md` recorded the Snowflake
connector as blocked on "the tenant, not the seam". That was true of attachment and false of
*mapping*. Both existing adapters name their source's fields in Python, which is right for them:
JSON exports and ORD were fixed formats before the adapters existed. Applied to a corporate
warehouse it means writing an adapter on the day access arrives and editing it every time a column
lands — the code change that the manifest seam was built to eliminate, reappearing one layer down.

The gap is specific. The seam answered *where a source comes from*. Nothing answered *what its rows
mean*, and for a source whose shape is unknown and mutable, that is the expensive half.

## Decision

**A generic warehouse engine, and the site's schema as a declarative binding inside its manifest.**

`chemclaw.ingest.eln.warehouse` provides both halves the seam expects — an `ElnAdapter` and a
`SourceRetriever` — and names no table and no column. Everything site-specific is the `binding:`
block of `config:`: the queries, the cursor columns, the child tables, and which column becomes which
field of `OrdReaction`. Attaching the real warehouse is writing that file. Adding a table is adding a
block.

Five decisions inside that, each rejecting a more general option:

**The binding is inline in the manifest, not a sibling file.** `registry._build_half` hands a half
its config kwargs and nothing else, so an adapter cannot learn its own folder to resolve a relative
path. Inline also keeps the property the seam exists for: a deployment mounts its own manifest
directory and its schema is never a change to this repository. Strictness moves into the binding's
own models, since `DataSourceManifest.config` is `dict[str, Any]` by contract.

**The transform vocabulary is closed.** Paths name one value; a small table of pure functions
reshapes it. No `eval`, no import from a transform name, unknown names rejected at load. A binding is
a file a deployment mounts; a transform that could reach code would make mounting an execution
surface. Not JSONPath and not an expression language either — a binding maps columns onto a fixed
schema, so every expression it needs is "one value, optionally reshaped", and anything more would let
a binding compute things the mapper has no field to receive.

**Unmapped columns get one bounded, typed home.** `OrdReaction` and `Component` gain
`attributes: dict[str, str]`, rendered at the end of the note body. Strings because these are
unmodelled by definition — and because amendment detection compares note bodies byte-for-byte, so
determinism matters. Ignored by `reaction_smiles`, `transformation_smiles` and both fingerprints, so
a structure can never enter the corpus through it.

**The vector table is searched in place, behind `SourceRetriever`.** The embedding is already there
over a corpus larger than what gets ingested; copying it here would mean re-embedding the larger
corpus forever. `NoteIndex` was the obvious seam and is the wrong altitude — `retrieval.retrievers`
drops hits with no note on local disk, which is every warehouse row. Chunks cite the row, as the
vendored dataset does, because a citation must resolve to something a reader can check.

**This ELN carries both halves, and the no-double-surfacing rule survives.** `eln-json` and `eln-ord`
are ingest-only because a file-drop ELN ingests everything it sees. A warehouse ELN ingests a curated
slice of something much larger, and the rest has no other way in. `suppress_ingested` drops a hit
whose reaction already became a note, so the agent sees reviewed knowledge for the curated part and
raw rows for the rest, never both for one reaction.

## Why not the alternatives

**Write the adapter when access arrives.** The honest option, and the one this rejects. It is not
that the adapter is hard — it is that it is never finished: every new column, every renamed view,
every child table the site adds is another change to Python, another review, another deploy. The
binding turns all of those into an edit to a file the deployment already owns.

**Generalize the ingest half instead.** `DEFERRED.md` carries "universal ingest abstraction" for the
day a non-reaction-shaped source arrives. This is deliberately *not* that: it generalizes the mapping
for one shape, leaving `IngestHalf = ElnAdapter` and the datetime cursor exactly as they were. The
trigger for that larger change is still a third real source, not this one.

**Add a `snowflake` extra to `pyproject.toml`.** There is no optional-dependencies table today, and
introducing one is its own decision with an image and chart story attached. The manifest seam already
gives the isolation an extra would buy: the driver module is imported only when a binding names it,
so the client stays out of the repository until a deployment installs it.

## Consequences

**Attaching the real warehouse is configuration.** Copy the shipped manifest into a mounted
directory, replace the names, set the environment variables it names, run
`make datasource-validate` — and `--construct`, added here, which builds the halves and so validates
the binding rather than just the keyword carrying it. No network, no tenant.

**It is not quite zero core edits, and that should be said plainly.** `attributes` on `OrdReaction`
and `Component` is a real change to the canonical model, and the note renderer changed with it. It is
small and bounded, and it is what buys "a new column is a line of YAML" — but the seam did not hold
perfectly, and the honest framing is that one field was the price.

**One consequence of `attributes` is behavioural.** Because dedup compares merged note bodies, an
amended lot number now re-proposes the note through the PR-gate. That is correct — an amendment is an
amendment — but it means a source carrying volatile columns in its attribute bag will generate more
PR traffic than one that does not. `exclude:` is the lever.

**The egress inventory grew by a name, deliberately.** `tests/test_no_egress.py` pins the discovered
source set, and `eln-snowflake` is now in it. D-089 was about third-party corpora; an internal ELN
behind the same identity boundary as everything else is a different thing. Two properties keep it
honest: the address and credentials come entirely from configuration (no host literal is permitted
anywhere in first-party code, and the same test file enforces that), and the source ships disabled.

**Four new error names joined the non-retryable list.** A malformed binding, a bad path, an unmapped
vocabulary value and a rejected query are all deterministic in something a retry cannot change. An
unreachable warehouse deliberately is not among them — the driver raises `ConnectionError` for that
case precisely so Temporal still retries it.

## Not in this change

The driver dependency, live credentials, the site's real binding, and user-scoped reads through
on-behalf-of. All four need a reachable tenant; `docs/planning/DEFERRED.md` now says exactly that and
nothing wider. A schema-introspection tool that drafts a binding from `INFORMATION_SCHEMA` was
considered and left out: it can only be written against a real warehouse's metadata, and writing it
against an imagined one is how it would end up drafting bindings nobody can use.
