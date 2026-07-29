# D-028 — Admin pluggability: ELN adapter registry, multi-dir skills, cache-trace log

**Context.** The admin audit's P1 findings: adding/switching an ELN source or a skills directory
meant editing code (the durable sync hardcoded `JsonExportAdapter()`, the memory jobs hardcoded
`[JsonExportAdapter(), OrdJsonAdapter()]`, `skills_dir` was a single string), and "why did this
recompute?" had no answer at the cache boundary.

**Decision.**
1. **One ELN adapter registry** (`eln/registry.py`): `ELN_ADAPTERS` maps a stable config name to
   each `ElnAdapter`. `make_eln_adapter(name)` picks one (clear error listing valid names);
   `all_eln_adapters()` returns the whole set. The durable sync's source is now
   `CHEMCLAW_ELN_SYNC_ADAPTER` (it tracks one high-water cursor, so it runs a single source — the
   deliberate deferral of running both under one cursor stands), and the memory jobs read
   `all_eln_adapters()` (the corpus is the union of every source). Adding a source is one registry
   entry, nowhere else — replacing the class names previously hardcoded in two workflow modules.
2. **Multi-directory skills** (`Settings.skills_dirs`): `CHEMCLAW_SKILLS_DIR` is now an
   OS-path-separator list (like `PATH`, e.g. `skills:/opt/team-skills`), read through the
   `skills_dirs` property that `FileSkillsSource` already accepts. An admin adds a second
   (e.g. team-private) skills directory with no code change and no JSON-in-env quoting. The
   SKILL.md front-matter schema + a template are now documented in `skills/README.md`.
3. **Cache-trace log**: `cached_compute` logs hit-vs-miss at DEBUG with the flat calculation key,
   the one place that answers "why did this recompute?" (behind the D-026 log-level switch).
4. **Runbook** (`docs/runbook.md`): the four recurring admin tasks (add a skill / add-or-repoint a
   DB / add-or-switch an ELN source / add a capability) + the troubleshooting surface (log switch,
   Temporal UI :8080, the DB-unreachable message).

**Why this shape.** Each change is a config switch over an existing seam — no new abstraction
without a real second caller (the registry genuinely serves both the pick-one sync and the
read-all memory jobs; the `skills_dirs` property has one consumer but matches the framework's
list signature and the audit's explicit ask). KISS/DRY intact, one config source, no new deps.
