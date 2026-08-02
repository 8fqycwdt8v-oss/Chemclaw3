# `chemclaw.core` — the shared kernel

**Responsibility:** the cross-cutting pieces every layer may import, and nothing else. Typed
configuration (`config`), the database pool (`db`), the HTTP client (`http`), id generation
(`ids`), structured logging (`logging`), the error taxonomy (`errors`), embeddings, reagent and
molecule helpers (`chem`, `reagents`), and the Temporal client factory.

**The rule that defines this package: `core` imports no sibling.** Not `agent`, not `durable`, not
`connectors` — nothing. Everything else builds on it, so a single edge the other way would make the
dependency graph a cycle and the four layers a suggestion. `tests/test_layering.py` runs each
kernel module in a clean interpreter and asserts each sibling is absent from `sys.modules`
afterwards — the module list is derived from disk, not maintained by hand, so an accidental import
fails as a named test rather than as a slow import at startup.

`config/` is the one `pydantic-settings` source for the whole system — every URL, path, threshold,
timeout and model name, `CHEMCLAW_`-prefixed and `extra="forbid"`. There is deliberately no second
config system anywhere, including in-cluster: the Helm `ConfigMap` keys mirror these field names
exactly.

It is a package of one module per domain section (the D-072 mixins), with the flat `Settings`
class composed — and the cross-section startup rules enforced — in its `__init__.py`. D-156
declined to fold that split into a restructure that was otherwise a set of moves, and noted it was
cheap whenever wanted because the import seam does not move; it was taken later, on exactly that
argument. One settings object, one import (`from chemclaw.core.config import settings`) — the seam
every call site uses, unchanged from the single-file era.
