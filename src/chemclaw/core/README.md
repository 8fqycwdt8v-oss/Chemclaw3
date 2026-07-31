# `chemclaw.core` — the shared kernel

**Responsibility:** the cross-cutting pieces every layer may import, and nothing else. Typed
configuration (`config`), the database pool (`db`), the HTTP client (`http`), id generation
(`ids`), structured logging (`logging`), the error taxonomy (`errors`), embeddings, reagent and
molecule helpers (`chem`, `reagents`), and the Temporal client factory.

**The rule that defines this package: `core` imports no sibling.** Not `agent`, not `durable`, not
`connectors` — nothing. Everything else builds on it, so a single edge the other way would make the
dependency graph a cycle and the four layers a suggestion. `tests/test_layering.py` runs each
kernel module in a clean interpreter and asserts each sibling is absent from `sys.modules`
afterwards: 11 modules × 12 siblings, so an accidental import fails as a named test rather than as a
slow import at startup.

`config.py` is the one `pydantic-settings` source for the whole system — every URL, path, threshold,
timeout and model name, `CHEMCLAW_`-prefixed and `extra="forbid"`. There is deliberately no second
config system anywhere, including in-cluster: the Helm `ConfigMap` keys mirror these field names
exactly.

It is also by some distance the largest file here (~1700 lines, 21 `Settings` classes). That is a
consequence of the rule, not a violation of it: one settings object means one file's worth of
fields, and splitting it into a package would buy browsability at the cost of the single import
seam (`from chemclaw.core.config import settings`, in 118 places) that makes the rule enforceable.
Considered and declined in D-154.
