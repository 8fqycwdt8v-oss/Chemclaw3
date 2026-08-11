# D-2026-08-11-the-observability-gap-is-real-and-langsmith-is-not-its-shape — LangSmith is declined; the gaps it would fill are named and split

**Status:** accepted · **Date:** 2026-08-11

## Context

Asked whether LangSmith is implemented here, whether it would bring value, whether it is open
source, and — if not — whether something comparable would be.

**It is not implemented.** `langsmith` 0.10.17 is installed, but only transitively: `deepagents`
requires `langsmith>=0.8.11` and `langchain-core` requires `langsmith>=0.3.45`. There is no
`LANGSMITH_*` or `LANGCHAIN_TRACING` setting in `core/config/`, in the chart, or in `.env.example`;
no import, no `Client`, no `@traceable`, no `evaluate()`. Before this ADR the word appeared in five
prose lines — two in `core/logging.py`, two in `docs/guides/runbook.md`, one in `deploy/README.md` —
all saying the same thing: that nothing in `langchain`, `langgraph` or `langsmith` emits per-model
token usage.

**It is not open source.** The client SDKs are; the backend, the UI and the storage layer are
proprietary. Self-hosting exists only as an Enterprise add-on behind a sales conversation, and it
needs external Postgres, Redis and ClickHouse. As of the May 2026 SmithDB announcement (a Rust
trace store on DataFusion/Vortex) the cloud and the self-hosted product no longer even run the same
storage engine — self-host was in early access. So "self-host it inside the cluster" is not an
option this repo can exercise on its own terms, which is the only kind of option its deployment
posture accepts.

**The gaps it would fill are real, and they are not one gap.** Separating them is most of this
decision:

1. **Per-model token and latency attribution.** A named regression: the removed framework emitted
   `gen_ai.client.token.usage` labelled by request model, response model, provider and token type,
   and nothing replaced it (`core/logging.py`, `runbook.md`, `deploy/README.md` all say so).
   `chemclaw_*_tokens_total` carries `profile` only, deliberately (D-152).
2. **Model-call spans.** There is nothing between `chemclaw.turn` and `chemclaw.tool`; the LLM call
   itself is invisible.
3. **Prompt and response content for debugging.**
4. **Dashboards.** None ship (`deploy/README.md`).
5. **Datasets, experiment diffing, annotation queues, online evaluators** — and this is the one with
   real pull, because **AG-13** (agent-behaviour / prompt / skill regression eval) has been deferred
   since D-057 for exactly the reason such a platform exists, and it currently blocks "the
   plan-vs-single-shot A/B has no real task set".

## Decision

**LangSmith is declined for the production path, and this is a decision rather than a deferral.**

Gap 3 is its core value proposition and is forbidden four times over in this tree, each time with an
argument this ADR does not improve on:

- `core/tracing.py`: "a span attribute travels to the collector, so the rule is the one `/metrics`
  follows: **identifiers and counts, never a question, an argument or an answer**."
- `core/logging.py`: "Nothing first-party puts content on a span."
- `SECURITY.md`: the audit trail's tool-call arguments "are user free text … and so **may contain
  PII or confidential chemistry**", and covering them is a policy obligation this code does not
  silently satisfy.
- D-049 chose **self-hosted Temporal over Temporal Cloud** specifically to "avoid egressing workflow
  payloads (which carry the Entra `oid`) to a third party". That is the same question with the same
  answer, and it is the closest precedent available.

Beside those: D-089 deleted a dormant external integration rather than leave it off-by-default, and
its host allowlist holds exactly one entry, so a config-only LangSmith wiring would pass
`tests/test_no_egress.py` on a technicality while contradicting its stated intent. The chart's
NetworkPolicy is default-deny egress, so a correctly configured deployment would need a new peer
added for `api.smith.langchain.com`. And D-2026-08-08 makes conversations erasable — an external
store of turn content needs its own erasure path, or that guarantee becomes false.

**Gaps 1, 2 and 4 are closed in-house, through the collector the chart already runs.** They do not
need a vendor and never did. The usage payload is already parsed at `api/runner.py`, so per-model
attribution is a label on a ledger that already carries model attribution (`turn_costs`) plus a
`chemclaw.model` span from a `wrap_model_call` middleware; dashboards are the operator's Grafana,
which the shipped ServiceMonitor/PodMonitor/PrometheusRule already feed. Backlogged, not built here.

Worth naming precisely because it is the genuinely useful, genuinely open half: the LangSmith **SDK**
can emit `gen_ai.*` OpenTelemetry spans to *any* OTLP endpoint (`LANGSMITH_OTEL_ENABLED=true` with
`OTEL_EXPORTER_OTLP_ENDPOINT` pointed at `otel-collector.observability.svc:4317`), with no LangSmith
backend, no credential and no egress. That is a real candidate for gaps 1 and 2 and it is on the
backlog as one — but it is **not a flag**, because those spans carry prompt and completion content by
default. Adopting it means deciding what is stripped and where, against `core/tracing.py`'s rule.
An integration that quietly inverted that rule would be worse than the gap.

**Gap 5 is the one where a platform would genuinely help, and it is scoped to the eval lane.**
`evals/live.py` already drives the real front door and writes a full event stream per probe to disk;
`evals/live_judge.py` already runs LLM-as-judge over `direction` with a stronger model than the one
under test. What is missing is dataset versioning, run-over-run diffing and annotation — the things
`baseline.json` plus Markdown reports approximate. Backlogged as a spike, restricted to that lane:
non-production, a dev credential, and probe questions that are already committed to this repo, which
is where the content objection is weakest.

**If that spike goes ahead, it evaluates the self-hostable tools first**, because LangSmith cannot be
self-hosted without an enterprise contract and a self-hosted platform is the only kind that could
later move toward real turns:

| | licence | what the licence gates | self-host footprint |
|---|---|---|---|
| **Arize Phoenix** | Elastic License 2.0 — source-available, **not** OSI | **nothing**; the three limitations are no-hosted-service-to-third-parties, no circumventing the licence key, no removing notices. Internal use and modification are explicitly permitted | one container + Postgres |
| **Langfuse** | MIT core, commercial `ee` tier | audit logs, data-retention management, server-side masking, project-level RBAC, SCIM, admin/instance APIs. SSO (incl. Entra ID) and org-level RBAC are in the core | Postgres + ClickHouse + Redis + S3 — four stateful services |
| **LangSmith** | proprietary; SDKs open | everything — the backend is not obtainable | Enterprise-only, sales-gated; Postgres + Redis + ClickHouse |

Both candidates are legally fine to run internally, so the licence *label* is the wrong thing to
decide on. What they gate is not: Langfuse's commercial set — audit logs, retention, server-side
masking — is exactly the compliance surface a GxP deployment would want, so "MIT core" describes the
licence rather than the deliverable, while Phoenix gates nothing. Against that, Phoenix's licence is
not OSI-approved, and an OSPO with a blanket ban on source-available licences ends the comparison
before any of it matters — a policy question rather than a legal one, and the first to ask.

Footprint points the same way: Phoenix adds one stateful dependency this cluster already runs, where
Langfuse adds three to a deployment whose ops story is Postgres plus Temporal. Phoenix is also
OTLP-first (the reference implementation of the OpenInference conventions), which matters because
the collector already exists. Langfuse's edge is the OSI licence and a deeper LangChain/LangGraph
integration.

Neither is adopted by this ADR, and none of the above is a measurement — it is a reading of two
vendors' own documents. The spike's exit criterion is whether it closes AG-13's stated blocker —
"needs an external benchmark + a live LLM to score it" — not whether the UI is nice.

## Consequences

- No new dependency, no new egress peer, no new stateful service. `langsmith` stays where it is: a
  transitive package nothing imports.
- The four written constraints above now have one place that reads them together, so the next time
  this question is asked it is a citation rather than a re-derivation.
- The three gaps that were being used to argue *for* a vendor are on the backlog as in-house work
  with a named mechanism, which is the honest state: they were never vendor-shaped, they were
  unfinished.
- AG-13 stays open. This ADR does not close it and does not pretend to; what it adds is that the
  route to closing it is a self-hosted eval platform on the eval lane, not production tracing.
