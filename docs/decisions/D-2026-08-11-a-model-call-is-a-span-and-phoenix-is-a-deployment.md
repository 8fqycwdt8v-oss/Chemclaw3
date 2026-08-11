# D-2026-08-11-a-model-call-is-a-span-and-phoenix-is-a-deployment — LLM spans through OpenInference, with content suppressed by default

**Status:** accepted · **Date:** 2026-08-11 ·
**Follows:** [D-2026-08-11-the-observability-gap-is-real-and-langsmith-is-not-its-shape](D-2026-08-11-the-observability-gap-is-real-and-langsmith-is-not-its-shape.md)

## Context

The LangSmith ADR split the observability ask into three parts and backlogged two of them as
in-house work: **per-model token and latency attribution**, lost when the agent framework left with
`gen_ai.client.token.usage` and named as a regression in `core/logging.py`,
`docs/guides/runbook.md` and `deploy/README.md`; and **the missing model-call span**, because
`core/tracing.py` opens exactly two spans — the turn and the tool call — so the LLM call between
them is invisible and a slow turn can be attributed to a tool or to "the rest of the turn" and no
further. It also recorded that if an eval platform were ever wanted, the self-hostable candidates
should be evaluated first, with Arize Phoenix the lighter of the two.

Phoenix was chosen. Working out what "implement Phoenix" means changed the shape of the change, and
that is most of this decision.

**Phoenix is a backend, and the client half is Apache-2.0.** `openinference-instrumentation-langchain`,
`openinference-instrumentation` and `openinference-semantic-conventions` are Apache-2.0; only the
Phoenix *server image* is Elastic License 2.0. The instrumentation emits OpenInference spans over
plain OTLP and never speaks to Phoenix, so pointing it at
`otel-collector.observability.svc:4317` — already in `values.yaml` — is the whole delivery
mechanism. Nothing ELv2-licensed enters this tree, and the licence question narrows to the container
an operator chooses to run, which is exactly where an OSPO can answer it.

That also means the two backlog rows are closed by the *same* change rather than by two, and that
"adopt Phoenix" is not a coupling: any OTLP backend receives the same spans.

## Decision

**Attach `LangChainInstrumentor` inside `configure_telemetry`, behind
`CHEMCLAW_OTEL_LLM_SPANS`, off in code and on in the shipped chart.** Off means the instrumentation
is not imported at all, so a deployment that does not want it pays nothing; on with the extras
missing raises the same directive `RuntimeError` the SDK check raises.

**Content is `otel_include_sensitive_data`'s decision, and that is how the flag stops being dead.**
It had exactly one consumer — the removed framework's instrumentation, which attached prompts and
results to its spans — and has spent a phase as a knob `configure_telemetry` warned about. This asks
the identical question, so it gets the flag back rather than a second one beside it. Off (the
default) builds an OpenInference `TraceConfig` with **every** hide flag set; the warning survives,
narrowed to the case where it is still inert (`otel_llm_spans` off).

Suppression is all-or-nothing on purpose. The eleven-plus hide flags answer one question — may turn
content leave this pod — and a deployment that answered it differently per attribute would be one
that had not answered it: a span carrying the prompt but not the completion still carries a
chemist's question. The list is written out rather than derived from the dataclass's fields, so a
hide flag a future release adds fails a test here instead of inheriting a decision.

## The measurement

**Suppression costs none of what the instrumentation was added for**, which is why it is a default
rather than a trade-off. A scripted model driven through a real compiled `build_langgraph_agent`,
spans collected in an `InMemorySpanExporter`, scanning *every* exported attribute value for the
question and the answer text:

| | spans | LLM spans | attributes carrying content |
|---|---|---|---|
| content allowed | 4 | 1 | **5** — `input.value`, `output.value`, two input-message contents, one output-message content |
| content suppressed (default) | 4 | 1 | **0** |

`llm.token_count.prompt`, `.completion`, `.total` and `llm.provider` are byte-identical across the
two runs. OpenInference's own `mask()` touches input, output, message, prompt, choice, embedding,
tool and invocation-parameter keys and nothing else, which is why.

A tool-calling turn — the shape a real turn has — exports **8 spans**: `AGENT` ×1, `LLM` ×2 (one per
model call, with their own counts), `CHAIN` ×4, `TOOL` ×1, and still zero content-bearing attributes
under suppression, including the tool's own arguments. Per-call rather than per-turn is precisely
what `chemclaw_*_tokens_total{profile}` cannot express and what the backlog row asked for.

**The content assertion is a sweep, not a list of keys**, and that is the design of
`tests/test_llm_spans.py` rather than a stylistic choice: naming keys tests the keys somebody
thought of, while a deployment's question is whether a chemist's question can reach the collector.
The sweep is also what would catch an upstream release adding a new content-bearing attribute.

**It has already earned that.** The first version of the suppression list omitted three embedding
flags, and `test_every_hide_flag_is_set_together` — which compares against the dataclass's own
fields rather than against a list written twice — is what found them. `hide_embeddings_text` is the
one that mattered: the text being embedded is a chemist's question or a note's body, so the omission
would have put content on a span under the configuration whose entire purpose is that it does not.

### Against a live Phoenix, not only an in-memory exporter

The above is measured on spans as they are *built*. What a deployment gets is spans as they are
*exported*, so the same turn was run twice through the shipped path — `configure_telemetry()`, the
real OTLP gRPC exporter, `CHEMCLAW_OTEL_ENDPOINT` — into Phoenix 20.0.0 running locally, and the
result read back out of Phoenix's own REST API:

| | spans | traces | token counts | content-bearing attributes |
|---|---|---|---|---|
| suppressed (default) | 10 | 1 | prompt 1234 / completion 56 / total 1290 | **0** |
| content allowed | 10 | 1 | prompt 1234 / completion 56 / total 1290 | **5** |

Identical in every respect except the one the flag governs. The five are `input.value`,
`output.value` and three message contents — the same five the in-memory run found, which is the
useful part: nothing is added or removed on the way through the exporter.

**The two span families join into one tree**, which was not obvious in advance and is the reason
this was worth running rather than reasoning about. Phoenix reports a single trace whose root is
*our* `chemclaw.turn`, with `chemclaw` (CHAIN) beneath it, then the skills middleware (AGENT),
`model` (CHAIN) → the model call (LLM) twice, `tools` (CHAIN) → `ask_clarifying_question` (TOOL),
and `chemclaw.tool` — ours, carrying `tool.name` — as a second child of the turn. The
first-party spans and the OpenInference ones share a trace id and nest, so an operator reading
Phoenix sees one turn rather than two disconnected halves.

Phoenix itself needed Python 3.12 to import (20.0.0 declares `>=3.10` and has a dataclass default
that only 3.12 accepts), so it was run in a venv of its own — which is the topology anyway, and a
small piece of evidence for the decision above that the backend is somebody else's process.

## Consequences

- `docs/guides/runbook.md` and `deploy/README.md` stop saying per-model attribution is gone. The
  replacement is not the metric that was lost: it is a *span*, so "which model, how many tokens" is
  a trace query while "what is this deployment spending per hour" stays `/metrics`. D-152's decision
  that `chemclaw_*_tokens_total` carries `profile` and not `model` is untouched and still right —
  `turn_costs` already holds per-turn model attribution, and a third answer would be a third thing
  to reconcile.
- The OpenInference `TOOL` span overlaps `core/tracing.py`'s `chemclaw.tool`. They nest rather than
  conflict — ours is opened inside the audit middleware, carries `tool.name`, is the one the audit
  trail ties to, and is the one that exists when this flag is off. Neither is removed.
- Nothing is guarded against double instrumentation: `configure_telemetry` already returns early on
  `_TRACING_INSTALLED`, and the instrumentor is a `BaseInstrumentor` singleton that logs rather than
  raising. A guard here would be a second answer to a question one flag already answers.
- The dependency's closure adds `opentelemetry-instrumentation`, `openinference-instrumentation` and
  `openinference-semantic-conventions`. No first-party module imports any of them directly.
- **This does not close AG-13.** That row asks for datasets, run-over-run diffing and annotation on
  the eval lane, and it stays open with Phoenix still the leading candidate — a *deployment* someone
  runs against the probe transcripts, not this instrumentation. What changed is that the trace half
  of the ask no longer needs a platform at all.
