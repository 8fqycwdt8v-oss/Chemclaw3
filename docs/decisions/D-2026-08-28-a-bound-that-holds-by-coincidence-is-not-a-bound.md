# D-2026-08-28-a-bound-that-holds-by-coincidence-is-not-a-bound — two metric declarations that described a property the code did not have

## Status

Accepted. An adversarial pass over `core/config/`, `core/metrics.py`, `core/logging.py`,
`core/db.py`, `core/bounded.py` and `durable/retention.py`. Two defects taken, four suspicions
measured and refuted; the refutations are recorded here because a negative result nobody wrote down
gets re-investigated.

## Context

`core/metrics.py` documents each label at its declaration, at length, and the documentation is the
only place a reader can learn what bounds a label's cardinality. Two of those paragraphs described
a property the code did not have.

**`chemclaw_repeated_tool_calls_total{tool}` took the model's own string.** The declaration read
"bounded by the registered tool surface, which is configuration (the enabled connectors and profile)
rather than anything a caller can name". `repeat_guard.count_call` booked
`request.tool_call["name"]`, and `ToolNode` invokes the whole `wrap_tool_call` chain for a name the
graph does *not* hold — that is deliberate, so an interceptor can short-circuit an unregistered
call. Measured, driving the real middleware three times with one invented name:

```
chemclaw_repeated_tool_calls_total{tool="totally_made_up_tool_ZZZZZ…"} 2
```

141 characters of model-authored text on the unauthenticated `/metrics`. This is the exact hole
`agent/audit.py::metric_tool_name` was written for and argued at length —
`/metrics` carries no identity, retrieved content is a prompt-injection surface this tree already
frames as untrusted, so "emit a tool call named `<secret>`" is an exfiltration channel, and the
128-series cap then blinds the metric permanently once the invented names fill it. `max_identical_tool_calls`
ships at 2, so three identical calls to a hallucinated name is the whole reproduction — a model
retrying a tool it invented is ordinary, not adversarial.

**`chemclaw_tool_results_truncated_total{tool}` took the same string**, under a declaration saying
the opposite ("a tool name here is one the registry served, never a string a caller invented"). Not
independently reachable: an unregistered call's result is the tool-node error message, far under the
60,000-character truncation ceiling, so the label happened to equal the registered name for every
call that could fire it. A coincidence is not the bound the declaration claims, and nothing held it
in place.

Two of five `tool`-labelled metrics, each with its own passing test asserting the label was safe —
because each test asked about one metric, and the question is about the *set*.

**A third, in the same shape one level up.** `chemclaw_connectors_unhealthy` counts down connectors
through `ConnectorHealth.unhealthy`, and the comment above it says the predicate lives on the model
"so this gauge and the `connectors_required` gate cannot drift into two definitions of the same
word". `chemclaw_connector_unhealthy` — the family whose entire job is to say *which* connector the
count is about — read `state == "unreachable"`, which is what the count read before
`D-2026-08-27-a-queue-with-no-poller-is-unreachable` added `unpolled`. Measured on a bundle whose
task queue has no poller:

```
chemclaw_connectors_unhealthy 1
chemclaw_connector_unhealthy{connector="durable"} 0
```

The alert fires and the "Connector reachability" panel an operator opens to answer *which one*
shows a healthy fleet. That is worse than the unbound family this metric replaced two weeks ago: an
empty graph reads as broken, a graph of zeroes reads as fine.

## Decision

**Every `tool` label resolves through `metric_tool_name`, and the registry is what enumerates the
producers.** `refuse_repeated_calls` books the counter (the decision function `count_call` stays
framework-free — the key must be the model's string, because two invented names are two different
repeats, while the label must be the graph's), and `bound_tool_results` labels with the clamped name
while the model-facing notice and the operator log keep the raw one, which is the forensic fact.

`metric_tool_name` loses its `name` parameter. Every caller passed
`request.tool_call["name"]` and the body never read it — a parameter whose value is exactly the
string the function exists to refuse reads like the clamp is a comparison, and it is not.

**`chemclaw_connector_unhealthy` uses `item.unhealthy`**, the same predicate as the count and the
gate. One word for one concept, in the one place the model already defines it.

**The tests are derived, because the per-metric tests are what missed this.**
`tests/test_tool_label_bound.py` drives every producer with one hallucinated name and asserts, in
both directions: nothing model-authored reaches the exposition, **and** every metric declaring a
`tool` label in `_COUNTER_LABELS`/`_HISTOGRAM_LABELS` was actually driven — so a sixth arrives with a
failing test rather than with a comment. A third case pins that a registered name still survives,
since every other assertion is satisfied by a registry that labels nothing.

**`tests/test_config.py::test_no_setting_anywhere_is_declared_without_a_consumer`** generalises
`test_no_calculator_setting_is_declared_without_a_reader`, whose docstring says the general form
cannot be written "because elsewhere a field with no first-party reader can be legitimate —
something a library or a chart consumes". That is true of a *reader in `src/`* and false of a
*consumer*: the chart consumes a setting by its `CHEMCLAW_`-prefixed environment variable, in files
this repository also owns. Counting attribute reads, string literals and `deploy/` text makes the
check exact with no allowlist — measured across every field the sections declare, the exempt set is
empty. Worth the generalisation because this tree has three times deleted clusters of settings
nothing read while `.env.example` parity stayed green throughout: the specialist and challenge-panel
seven (D-2026-08-15), the three compaction settings whose policy lived in a removed framework
(D-2026-08-11), and the fourteen `hpc_*` fields
(`D-2026-08-26-semiempirical-is-the-whole-tier`).

## What was measured and refuted

Recorded so the next pass starts from a number rather than from the same suspicion.

- **No setting is orphaned.** All fields the `*Settings` sections declare are consumed; the eleven
  with no `src/` attribute read are consumed by a validator, a derived property, the chart's
  `config.yaml`/`_helpers.tpl`, or `deploy/entrypoint.sh`. Every `@property` in `core/config/` has a
  reader outside its own module.
- **No metric lacks a producer.** Every declared counter, gauge, histogram and gauge family has a
  producing call site; the seven with no *literal* one are the four priced token counters
  (`api/runner.py` loops over a tuple), `_DEGRADED_COUNTER`, and the two the registry emits about
  itself, each already held by a test in `tests/test_metric_declarations.py`.
- **No other label is unbounded.** `route` is the FastAPI route template with `<unmatched>` as its
  only fallback; `operation` and `kind` are source literals; `source`, `sink`, `connector` and
  `subsystem` are registry or manifest names; `activity` is a registered activity type. `observe`
  caps series at 128 exactly as `increment` does.
- **The redaction inventory is complete.** No `Settings` field matching `(api_key|token|secret|
  password|dsn|credential)$` sits outside `_SECRET_SETTINGS`, and no field carries a credential
  under a name that pattern misses; `tests/test_credentials.py`'s three directions already close the
  loop in every combination.
- **`BoundedLru` at `cli/mock_llm.py`'s 20,000 is not close to biting.** Simulated at the real
  access pattern — one response id minted and continued per model call — **60,000 turns lose zero
  continuations**. Loss needs more than 20,000 *simultaneously unresolved* ids, i.e. 20,000
  concurrent in-flight turns against one mock process, against a shipped admission cap of 8. What
  a miss would do is worth knowing anyway and is written in `MockLlm.select`: it falls back to the
  default behaviour rather than failing, which is the LOAD-1 shape that docstring names.
- **Retention's ordering holds.** `_PRUNABLE` is iterated in insertion order with `session_owners`
  last and every session-scoped table ahead of it; `_untouched_arms` is built once for both the
  candidate query and the `DELETE`, so the two cannot ask different questions; the three checkpoint
  tables go in one transaction rather than in `_PRUNABLE` order, and `_prune_checkpoints` argues
  why. `_NOT_PRUNED` plus `_PRUNABLE` name every table the migrations create, and
  `tests/test_retention.py` asserts it in both directions.

## Consequences

- `count_call` no longer emits a metric; nothing else called it, and the two test modules that drive
  it directly assert the decision rather than the counter. `tests/test_repeat_guard.py::_ctx` now
  carries the registered tool object, which is what `ToolNode` passes for a name the graph holds and
  what the measured loop (7-8 `find_past_jobs` calls) actually was.
- `agent/repeat_guard.py` and `agent/tool_result_size.py` now import `agent/audit.py`. Both are
  already in the same middleware chain and there is no cycle; the alternative — a third spelling of
  the clamp — is the thing this decision exists to prevent.
- A deployment that had been reading `chemclaw_connector_unhealthy` sees an `unpolled` bundle change
  from 0 to 1. That is the correction, not a regression: the count beside it has said 1 since
  2026-08-27.
