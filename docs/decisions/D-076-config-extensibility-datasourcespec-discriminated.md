# D-076 — Config-extensibility: `DataSourceSpec` discriminated union (audit doc 10, item 4)

**Context.** The data-source seam (`sources/registry.py`) was structurally good — `{name: factory}` +
one config token — but had **no per-*instance* config**: a "type" was just a registry key bound to a
factory reading flat globals, so the single global `eln_export_dir` served every JSON-ELN source and
two instances of one type (prod + staging, different directories) were impossible (audit §2.3). The
audit (§5) recommended a scoped pydantic discriminated union carrying per-instance config, additive
to the comma-string token, reusing two in-repo idioms (`config.McpServerSpec` typed list +
`bo/problem.py:57`'s `Field(discriminator=…)`).

**Decision.** `DataSourceSpec = Annotated[JsonElnSourceSpec | OrdElnSourceSpec, Field(discriminator="type")]`
in `config.py` (beside `McpServerSpec`), plus an additive `data_source_specs: list[DataSourceSpec]`
token in `SourcesSettings`. `sources.registry.build_data_source(spec)` dispatches `type → adapter`,
each variant nesting its own `export_dir`. `_active_sources()` now builds the comma-list sources then
the spec sources; consumers (`gather_evidence`, the ELN sync) are untouched — they still iterate built
`DataSource`s. Keyless/default sources stay in the comma list (no regression).

**Temporal boundary kept string-keyed.** `sync_eln_entries(source: str)` still calls
`make_data_source(name)`; that resolver now falls through to spec-by-name after the built-in keys, so
in-flight workflow histories stay byte-identical (durability > signature elegance, audit §5).

**Real second caller, no stub.** Both ELN adapters already accept an `export_dir` constructor arg, so
the two variants expose an existing, working parameter per-instance — delivering the "two instances /
different dirs" capability with **zero** speculative code. The Snowflake connector (nesting
connection/credential-ref/schema-mapping config, the first `exchange_obo` caller) stays deferred and
joins as one more variant + one `build_data_source` branch when it lands (DEFERRED.md).

**Deliberate deviation from audit §5 (KISS / DRY).** Dropped the proposed near-empty
`RegisteredSourceSpec` bridge variant: it would duplicate the comma-string token (the §2.4 "two ways
to configure a list" friction) and introduce double-build/collision ambiguity between the two tokens.
The two real ELN variants already make it a genuine discriminated union, so the bridge variant was
ceremony without a caller.

**Invariant preserved (fail-fast).** Names are unique across both tokens (a shared name = a shared
`sync_cursors` row, so one cursor could skip the other's entries) — a startup `@model_validator`; and
a spec reusing a built-in registry key (which `make_data_source` resolves first, silently shadowing
the spec) is a loud error in `build_data_source`, not a sync-time surprise. RBAC/audit/PR-gate are
untouched — the seam only changes how a `DataSource` is *built*, never how its ingest is gated.
