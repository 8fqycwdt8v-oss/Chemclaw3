# D-2026-08-02-the-seam-does-not-move — `core/config` becomes a package, the import seam stays

**Status:** accepted · **Date:** 2026-08-02 · **Implements:** the split D-156 declined to bundle
and explicitly priced as "cheap whenever it is wanted" · **Builds on:** D-072 (the section mixins)

## Context

`core/config.py` had grown to 2157 lines, 310 fields and 20 section mixins, imported by 143 source
files and the most-churned file in the repository — every package's feature work adds a field, so
one file serialized all parallel work. D-072 already gave each domain its own mixin; D-156
considered splitting the file into a package, declined to mix it into a restructure that was
otherwise a set of moves, and recorded why the split stays cheap: every import site says
`from chemclaw.core.config import settings`, so the seam does not move. This ADR pulls that
trigger. `core/README.md` previously summarized D-156 as "considered and declined"; a reader needs
the record of when and why that flipped, which is why this is an ADR and not just a diff.

## Decision

`core/config.py` is now the package `core/config/`:

- **One module per D-072 mixin**, named for its section: `observability`, `temporal`, `store`,
  `hpc`, `calculators`, `bo`, `llm`, `agent`, `service`, `entra`, `kg`, `evals`, `fingerprints`,
  `eln`, `sources`, `connectors`, `memory`, `retrieval`, `reports`, `safety`. Class bodies moved
  verbatim — the split was performed mechanically from the AST so no field, default, validator or
  comment could drift in transit. No mixins were merged: every D-072 boundary held up as a real
  domain.
- **`__init__.py` keeps the whole public surface**: the composed `Settings` (same mixin order,
  so the MRO is unchanged), the `settings` singleton, and `NOTE_INDEX_SOURCES` (which lives in
  `retrieval.py`, beside the note-index sources it describes). The two **cross-section
  `@model_validator`s stay on the composed class** in `__init__.py` — deliberately: each enforces
  a rule spanning several sections (fleet admission ceiling, mid-turn resume vs turn timeout,
  budget caps, the `service_uvicorn_workers>1` refusal; the connector-job ceiling vs the QM poll
  budget), and no single section can see both sides. A per-section rule stays in its section, as
  before.
- **`shipped.py`** holds `_shipped`/`_PACKAGE` (D-148), shared by the three sections whose
  defaults name in-package declarations. Moving it one package deeper changed the `__file__`
  arithmetic (`parents[2]`, not `parent.parent`) — the one line in the package that could not move
  verbatim.

## Consequences

- **Zero call-site edits.** Every `from chemclaw.core.config import settings` (and the handful of
  `Settings`/`EvalSettings`/`NOTE_INDEX_SOURCES` imports) resolves exactly as before.
- **Behavior proven identical, not asserted**: the full field inventory (name, annotation,
  constraint metadata, constructed default), the MRO, the registered model validators and every
  derived property were dumped from `Settings(_env_file=None)` before and after — byte-identical.
  The only observable difference is incidental namespace leakage: `os`, `Path`, `Field`,
  `Literal`, `BaseSettings` are no longer attributes of `chemclaw.core.config`, and the section
  modules are. Nothing imported those names from here.
- Sessions adding a field now touch one section module instead of the one file everything else is
  touching — the churn decoupling this refactor exists for.
- `tests/test_layering.py` derives its module list from disk, so the kernel no-sibling rule now
  covers each section module individually, with no new package edges.
- Docstring pointers that named `core/config.py` (a file that no longer exists) now name
  `core/config/`, per `tests/test_docstring_paths.py`'s rule that prose names the file as it is
  now.
