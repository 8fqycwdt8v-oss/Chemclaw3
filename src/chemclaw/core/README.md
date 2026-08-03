# `chemclaw.core` — the shared kernel

**Responsibility:** the cross-cutting pieces every layer may import, and nothing else. Typed
configuration (`config`), the database pool (`db`), the HTTP client (`http`), id generation
(`ids`), structured logging (`logging`), the error taxonomy (`errors`), embeddings, reagent and
molecule helpers (`chem`, `reagents`), and the Temporal client factory.

Four **ambient-turn primitives** live here too, and their common property is why: each is a
`contextvar` (or a name-keyed dict) over plain values, importing nothing but the standard library,
and each is read from several sibling packages at once — a count worth measuring when it matters
(an AST or grep pass over `src/`) rather than pinning here, since it moves every time a capability
grows and a stale range is exactly the kind of claim this file exists to not make. The turn's
identity (`identity_context`), its session id
(`session_context`), the side-channel a tool records job launches and PR-gate proposals on
(`turn_signals`), and the in-process capability-tool registry (`tool_registry`). They sat in
`chemclaw.agent` until R2 and were the single import that made three sibling edges — including one
whole `kg <-> agent` cycle — exist at all.

`metrics` is the process-wide Prometheus registry, here for the same reason: a scrape targets a
*process*, and every process in the system has something to count. **It is not the eval layer's
metrics.** `evals/metric.py` is the `@metric` decorator and registry for scored eval criteria and
`evals/metrics.py` the seed criteria themselves; three files, one word, no relationship. `metrics`
here counts turns, tokens and jobs for an operator.

**The rule that defines this package: `core` imports no sibling.** Not `agent`, not `durable`, not
`connectors` — nothing. Everything else builds on it, so a single edge the other way would make the
dependency graph a cycle and the four layers a suggestion. `tests/test_layering.py` runs each
kernel module in a clean interpreter and asserts each sibling is absent from `sys.modules`
afterwards — the module list is derived from disk, not maintained by hand, so an accidental import
fails as a named test rather than as a slow import at startup. The static half of the same test
walks every first-party import: `core` has **no module-scope edge to any sibling at all**, and
exactly one declared lazy exception (`logging`'s redaction filter resolving connector token env
names).

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
