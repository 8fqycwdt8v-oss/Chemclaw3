# `chemclaw.core` — the shared kernel

**Responsibility:** the cross-cutting pieces every layer may import, and nothing else. Typed
configuration (`config`), the database pool (`db`), the HTTP client (`http`), id generation
(`ids`), structured logging (`logging`), the error taxonomy (`errors`), embeddings, reagent and
molecule helpers (`chem`, `reagents`), and the Temporal client factory.

Several **ambient-turn primitives** live here too, and their common property is why: each is a
`contextvar` (or a name-keyed dict) over plain values, importing no sibling package — mostly
nothing but the standard library (`turn_signals` writes through LangGraph's stream writer) — and each is read from several sibling packages at once. The turn's identity (`identity_context`),
its session id (`session_context`), the side-channel a tool records job launches and recorded notes
on (`turn_signals`), the in-process capability-tool registry (`tool_registry`), the turn's boolean
flags (`turn_flags`), the chemist's own words in the thread (`turn_text`) and the plan step a tool
call is linked to (`plan_context`). Keeping them out of `chemclaw.agent` is what removed the sibling
edges — including a whole `kg <-> agent` cycle — they used to create.

`fulltext` is here for a narrower version of the same reason: it holds the *one* lexical boolean
rule — the widened tsquery both durable indexes join against, and the tokenizer both their offline
references match with. The two indexes live in different packages (`retrieval.vector_index`,
`ingest.documents.index`), `core` is the only package both already depend on, and every time that
rule has been written twice the two copies have disagreed silently. `db` holds the dense half of
the same story: `apply_vector_recall_settings` is the pgvector recall parameters *both* dense
searches run under.

`markdown` is the same argument at a third scale: one Markdown table renderer for every package
that emits one, holding the three rules a hand-rolled table gets wrong — a `|` cannot add a cell,
absence has exactly one spelling, and whether a zero-row table renders at all belongs to the caller.
It imports nothing but `collections.abc`.

`authorship` is the one answer to "who wrote this" (`D-2026-09-27-an-author-is-a-person-and-an-agent`):
the person it was written for and the agent that wrote it, which a knowledge note, an audit row and a
transcript message all store under the same two names. It is here for the `fulltext` reason — `kg`,
`agent` and `api` each read it, and three corners answering the question separately is the failure
it was decided once to prevent.

`connect` is the one way to attach a database this system does **not** own, and it is here for the
`fulltext` reason rather than the `db` one: three seams reach somebody else's database — the
warehouse ELN inbound (`ingest`), the result store outbound (`publish`), and the dense half of
retrieval (`retrieval.vectors`) — `core` is the only package all three already depend on, and each
time that logic was written separately the copies diverged. It resolves a `module:callable` driver
late, reads every `*_env` key from the environment at connect time and registers the name for log
redaction first. **It enumerates no vendor's connection fields**: the driver's signature is the
schema (`D-2026-08-26-the-driver-s-signature-is-the-schema`), which is what keeps a lakehouse, a
Postgres and a vector database from having to share one model. It takes the exception class as a
parameter because Temporal matches non-retryable errors by class *name*, so each seam keeps its own.

`metrics` is the process-wide Prometheus registry, here for the same reason: a scrape targets a
*process*, and every process in the system has something to count. **It is not the eval layer's
metrics.** `evals/metric.py` is the `@metric` decorator and registry for scored eval criteria and
`evals/metrics.py` the seed criteria themselves; three files, one word, no relationship. `metrics`
here counts turns, tokens and jobs for an operator.

`llm_gateway` is one *policy*, not a primitive, and the reason it lives in the kernel is the reason
the kernel exists. It refuses to boot a process pointed at a loopback model gateway, which is a
question every process that takes a turn has to answer — the front door, the read-only MCP face, the
background worker whose agent activity builds a graph, and the terminal CLI. It was written in
`api/middleware.py`, where `create_app` was its only possible caller, so three of those four ran
without it (`D-2026-09-12-a-gateway-guard-in-the-front-door-is-not-a-deployment-guard`). It reads
`config` and `http.is_loopback_url` and nothing else, so no edge is created by it being here.

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
class composed — and the cross-section startup rules enforced — in its `__init__.py`; see
`config/README.md`. One settings object, one import (`from chemclaw.core.config import settings`).

## The other kernel modules

| Module | What it is |
| --- | --- |
| `aio`, `executor` | async primitives that survive several event loops in one process; the one sized thread pool every `asyncio.to_thread` shares |
| `asgi`, `worker_http` | shared pure-ASGI middleware; the scrape and probe surface for a process that is not the front door |
| `bounded` | the one bounded LRU map for every cache keyed by an unbounded identity |
| `call_identity`, `mcp_session` | the turn's identity as outbound headers for one origin; the one outbound MCP client session |
| `temporal_client` | the one place a Temporal client is opened |
| `checkout` | whether a path is the git checkout this process runs from |
| `egress`, `netguard`, `netguard_preload` (+ `netguard_preload.c`) | the LangSmith content-egress decision, and the in-process and compiled egress guards |
| `migrate`, `grants` | apply `infra/sql/` migrations (`make db-migrate`) and reconcile the runtime principal's privileges (`make db-grants`) |
| `jsonb`, `manifest_io` | the one `jsonb` write wrapper; the one manifest reader and its rule for fields that execute |
| `metrics_bridge`, `tracing` | metric updates that cannot break their caller; first-party spans and their cross-process propagation |
| `model_prose` | the marker a module-level string carries when a model is sent it |
| `units`, `quantities` | the one restricted unit registry and `Measurement`; matching the numbers a prose answer states against a payload's |
| `result_handle` | the handle at the foot of a tool result, and how readers of numbers skip it |
| `turn_cost` | the shape of one turn's spend record |
