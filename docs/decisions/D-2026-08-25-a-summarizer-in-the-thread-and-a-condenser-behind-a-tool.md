# D-2026-08-25-a-summarizer-in-the-thread-and-a-condenser-behind-a-tool — two different decisions about the same model call

**Status:** accepted · **Date:** 2026-08-25 · Does **not** supersede D-025 or
[`D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has`](D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has.md) — it distinguishes them.

## Context

This repository declines LLM summarization, twice, in writing. `agent/compaction.py` says it in as
many words — *"a summarizer reads retrieved evidence and writes text that is then replayed as
conversation, so it is an indirect-prompt-injection surface pointed straight at the thread"* — and
`disabled_summarizer` occupies upstream's slot with `trigger=None` so that
`create_deep_agent`'s unconditional `SummarizationMiddleware` can never fire.
`tests/test_compaction.py::test_the_summarizer_in_the_compiled_stack_can_never_fire` pins it.

`condense_protocols` makes an LLM call over retrieved evidence. It looks like a reversal. This ADR
exists because a future reader will reach for the prohibition by name, and needs to find the
distinction beside it rather than infer it.

## Decision

**The declination is about the conversation thread, and it stands untouched.** Its two stated
reasons are the *replay* and the *envelope*, and neither describes a tool.

| | summarizer output | `condense_protocols` result |
|---|---|---|
| enters the thread as | a rewritten message list, in the model's own voice | a `ToolMessage` |
| `agent/framing` envelope | destroyed by the rewrite | re-applied on the way out |
| re-read on later turns | yes, unavoidably — it *is* the history | no; `ClearToolUsesEdit` clears it like any other result |
| audited (`audit_tool_calls`) | no | yes |
| authorized (`enforce_tool_authz`) | no | yes |
| dry-run refused, repeat-guarded | no | yes |
| citable | no | yes, per row |
| withdrawable | no | yes — one name out of the registry |

A summarizer launders an injected instruction into the model's own voice and then replays it every
turn. A tool result is data the model is told to read as data, crossing every gate, carrying the
citations that make it checkable, and discardable.

**The claim is asserted by a test, not by this table.** `tests/test_protocol_condense.py` drives a
real compiled graph and asserts the call lands in the audit trail with its actor and correlation id
— because "it crosses the chain" is only an argument if the call actually does — and pins beside it
that the summarizer is *still* unable to fire. Both facts in one place, so the pair is legible.

**Nothing in the compaction policy changes.** `disabled_summarizer` keeps `trigger=None`, the
existing test stays green, and `agent/compaction.py`'s docstring gains a pointer here, because that
docstring is what a future reader will cite as the prohibition and it must not be left arguing
against something it does not cover.

## The condensation itself

**The reduce is deterministic and already existed.** `memory/comparison.py` is the table
`optimization_campaign_note` has rendered since Phase 5, extracted so a second caller can use it:
a row per protocol, conditions and outcomes side by side, and what each changed relative to the one
before. The turn-time comparison and the PR-gated campaign note are now one artifact at two
altitudes rather than two tables that can disagree.

**The map is a model call only where the record is prose.** The split is not "structured versus
unstructured" — it is *does the record reach this process as a model or as sentences*. On the
background worker it is an `OrdReaction` and the map needs no model at all. In the chat pod
`OrdReaction` is unreachable (see the companion ADR), so the numbers come from note frontmatter and
the model is asked only what the prose can answer: solvent, reagents with loadings, work-up,
observations, and one verbatim line. Every field may come back null, because absent is a legal
answer and inventing a number to fill a column is the failure the whole artifact exists to avoid.

**A protocol is never split, and one too large is refused by name.** Head-truncating would be worse
than refusing and not by a little: a procedure states its yield and purity at the *end*, so a
truncated read returns a row whose conditions look complete beside an outcome that is silently
absent — reading as "not measured" against neighbours that measured it. That is the fabrication
`_quality_columns` drops a whole column to avoid. A row saying "41,200 characters, not read" sends
a chemist to the right document instead.

**Degradation is per protocol, never per turn.** A failed or timed-out extraction keeps that row's
recorded figures and says its procedure was not read; with no reachable route at all the comparison
still renders from the records. That is what lets the tool ship on with no credential present — a
capability that ships off is not a capability (`D-2026-08-15`).

**`complete` means every reference the caller passed was read.** It never means "you have seen every
protocol on file"; conflating those is the `FingerprintSearch.verdict` failure, and the tool
docstring and both skills say so.

## Measured

Against `expand_note` once per protocol, on a 2.8 kB protocol — the middle of the 3–8 kB band an ELN
procedure occupies:

| N | `expand_note` × N | `condense_protocols` | ratio |
|---|---|---|---|
| 5 | 3,600 | 472 | 7.6× |
| 20 | 14,410 | 1,648 | 8.7× |
| 80 | 57,650 | 6,352 | 9.1× |

How many protocols fit the 100,000-token budget on tool results alone:

| protocol size | via `expand_note` | via `condense_protocols` |
|---|---|---|
| 2.8 kB | 137 | 1,455 |
| 8 kB | 49 | 1,455 |

The structural property is the second table rather than the first: **the condensation's marginal
cost per protocol is independent of protocol size**, because a digest row is bounded. So the budget
bounds how many protocols a turn can hold rather than how long they happen to be.
`protocol_digest_max_protocols` binds first at 24 — that cap is about how many model calls one turn
should make, not about output size.

The first implementation measured **1.4×** and would not have been worth building: `Condensation`
serialized the rendered table *and* the structured rows it was rendered from, so every extracted
field went out twice. `rows` is now excluded from serialization and kept on the object.

## Consequences

- One more LLM call site in the tree, the third (`agent/langgraph_agent`, `agent/verifier`, this).
  It follows the verifier's five properties exactly, including `method="json_schema"` — the default
  `function_calling` path drops defaulted fields out of `required`, measured at 8/8 failures there.
- `chemclaw_protocol_digests_total{outcome}` and the `protocol_digest` degraded subsystem exist
  because a condensing endpoint that is down otherwise looks exactly like a corpus of protocols
  whose procedures happen to be empty.
- `number_change` becomes public and `text_change` joins it in `memory/progression.py`: a real
  second caller now exists, and two copies would render `90 °C → 70 °C` in the campaign note and
  something subtly different in the comparison a chemist reads beside it.
