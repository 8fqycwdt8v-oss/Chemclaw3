# D-2026-08-29-a-helpers-report-is-model-prose-in-its-callers-thread — the two controls that say "every tool" now reach the one that returns a Command

**Status:** accepted · **Date:** 2026-08-29 · Extends
`D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller`, which narrowed what a helper may
*do*. This is about what a helper *returns*. Applies
`D-2026-08-25-a-summarizer-in-the-thread-and-a-condenser-behind-a-tool`'s rule to the one span it
had not been applied to.

## Context

The previous change asked what a helper reaches. This one started from the other end — whether the
isolation a helper exists for is real — and the good news is measured first, because it is the
premise everything else rests on and nothing asserted it.

**Isolation holds.** Driven on a compiled graph with a scripted helper that reads a ~9.8 kB payload
and reports back: the caller's whole thread is **57 characters** — the `task` call and a 28-character
report. The helper's intermediate tool results never enter it. That is a property of the plumbing
rather than of a model, which is why a scripted helper is evidence about the system here and not
merely evidence about a fake; `tests/test_subagents.py` now pins it.

**What the same probe exposed is that a helper's report is model prose entering its caller's thread
with nothing applied to it.** Two controls both say, in their own docstrings, that they cover every
tool. Neither covered this one, and the cause is a single shape: **`task` returns a
`langgraph.types.Command`, not a `ToolMessage`** — because a helper has to write its report *and*
the channels that cross the subagent boundary (`model_calls`, `billed_tokens`, its `files`) into the
caller's state in one act. Measured, the object reaching the tool middleware chain is
`Command(update={'files': …, 'model_calls': …, 'messages': [ToolMessage(…)]})`, and both middlewares
open with `if not isinstance(result, ToolMessage): return result`.

**1. The report was not defanged.** Measured: a report containing `</retrieved-note-…>` reached the
caller's thread carrying a **live** closing delimiter, so everything the report wrote after it read
— to the caller's model — as text outside any envelope. The nonce does not cover this the way it
covers external content: `frame_untrusted`'s own docstring says "forgery is closed by *defanging*
the content, and the nonce and the defang each cover the other's gap", and a helper does not have to
*guess* the tag. It has just read it, in the envelopes around its own evidence. Every other route by
which model prose or third-party text reaches a prompt already neutralises it — `agent/condense.py`
defangs each field the digest model returns, `agent/verifier.py` defangs the answer under review,
retrieved content is framed at its source. This was the one span that arrived raw, and it is exactly
the laundering `D-2026-08-25` declines a summarizer for.

**2. The report was not bounded by this repository's ceiling**, and there is a band where nothing
bounded it at all. Upstream's `FilesystemMiddleware` evicts a result over
`tool_token_limit_before_evict` (20,000 tokens × 4 chars = **80,000**) to `/large_tool_results/`;
`agent_max_tool_result_chars` is **60,000**. Measured across that band: a 180,048-character report
was offloaded by upstream to 1,599 characters, and a **70,048-character report reached the caller's
thread whole**.

Neither is a hole in a gate — the helper's tool calls cross the audit trail, the authorization
chain, the dry-run refusal and the plan gate exactly as its caller's do. Both are controls that
believed they were general and were not.

## Decision

**`agent/tool_result_shape.py` holds one function, `rewritten_tool_messages`, and both middlewares
go through it.** It applies a `ToolMessage → ToolMessage` rewrite to whatever shape a tool returned:
a bare message, or a `Command` whose `update["messages"]` carries them — rebuilding the command with
**every other key of the update preserved**, because those keys are how a fan-out's spend reaches
the one budget it shares. A rebuild that kept only `messages` would take that off the ledger
silently, which is `tests/test_state_channels.py`'s whole subject: a write the graph never sees.

One module rather than two edits, and that is the point rather than tidiness: a third middleware
that rewrites a result reaches for the same function and inherits the coverage, where a second copy
of the `isinstance` guard would inherit the hole.

**A helper's report is defanged, not framed.** An envelope tells the model a span is evidence to
weigh and cite, and a helper's summary is neither — citing it would credit a source that is this
system's own paraphrase. That is the distinction `frame_connector_results` already draws for a
connector's *error* text, applied for the same reason.

`subagent_tool_names()` is now `@cache`d and returns a `frozenset`: it answers a question about the
installed package by *building* a `SubAgentMiddleware`, which is cheap once and wasteful on a path
that now runs per tool call.

## Consequences

- Measured after: the delimiter arrives as `&lt;/retrieved-note-…>`, and a 70,048-character report
  arrives as 60,312. Both new tests were **verified to fail** against the pre-fix behaviour before
  being kept.
- **What a caller still cannot see is that the report is derived from untrusted reading.** Defanging
  closes the mechanical hole; the caller has no marker saying "this prose came from evidence that
  arrived enveloped". That is an epistemic gap rather than a mechanical one and it is a
  `docs/planning/BACKLOG.md` row, not a silent omission.
- **The isolation premise is now asserted rather than believed**, with the ratio rather than the
  fixture's byte count as the claim: everything a helper reads costs its caller only the report.
- The delegation measurement stays open and stays a live-model question. Nothing here needed one:
  every number in this ADR is a property of the graph, which is why it could be taken at all in an
  environment with no model to call.
