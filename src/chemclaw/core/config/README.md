# `chemclaw.core.config` — the one typed config source

One `pydantic-settings` object for the whole system, split into one module per domain section:
each module holds one section mixin (its fields, its own validators, its derived properties), so
everything about one concern sits in one file. D-072 drew the section boundaries; the split into a
package executed the escape hatch D-156 left open.

`__init__.py` composes the mixins into the flat `Settings` class, owns the `CHEMCLAW_` env prefix,
the `.env` loading and `extra="forbid"`, holds the cross-section validators (the rules no single
section can see both sides of), and exports the `settings` singleton. `shipped.py` holds the shared
helper for defaults that name declarations shipping inside the installed package (D-148).

The import seam is unchanged from the single-file era — `from chemclaw.core.config import
settings`, everywhere — and so are the field names, env names and defaults, which
`.env.example` mirrors in both directions (`tests/test_config.py`).
