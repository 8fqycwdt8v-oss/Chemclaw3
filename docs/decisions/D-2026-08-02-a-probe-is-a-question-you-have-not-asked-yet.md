# D-2026-08-02-a-probe-is-a-question-you-have-not-asked-yet — A probe is a question you have not asked yet

**Status:** accepted · **Date:** 2026-08-02 · **Closes:** AG-13 (agent-behavior eval against a real
LLM) · **Extends:** D-009 (the eval/metric layer), D-138 (the fifty-question live pass),
D-2026-08-01-a-scripted-transcript-gates-the-harness-not-the-judgment

## Context

`evals/autonomy.py` states the limit in its own module docstring: its transcripts are scripted, the
model's replies are pinned, and so *nothing about the model's judgment is under test*. That is true
of every behaviour test in this repository. `tests/test_harness_execution.py` goes furthest — a real
MAF chat client with the real middleware layering — and still only the model's replies are scripted.
The result is a suite of 2,009 tests that gates the harness around the model and never the model.

`DEFERRED.md` named the gap as AG-13 and parked it on a reachable endpoint. Meanwhile the actual
live testing happened by hand and survived only as prose: `docs/archive/vibe-test-2026-07.md`
(fifty questions, five defects, **four of them invisible to 1,450 passing tests**),
`live-matrix-2026-07.md`, `load-test-2026-07.md`. Each one found real defects and left behind no way
to re-run itself. A finding you cannot reproduce is a claim, and a method you cannot re-run is not a
gate.

## Decision

**A `Probe` is its own type, not an `EvalCase`.** An `EvalCase` scores a value that has already been
produced — a pure function over recorded output. A probe is the *input* to a conversation that has
not happened yet, plus the evidence that would make its answer acceptable. Folding them together
would put an HTTP round trip inside a pure metric, and the two are read by different machinery.

**The runner drives the HTTP/SSE front door, never `build_agent()` in-process.** The in-process
agent skips identity, authorization, budget admission, the audit sink, the durable session store and
the streaming assembler that rebuilds tool calls from name-first fragments. Three of D-138's five
defects lived in exactly that layer: tool-call events carrying no arguments, a failing tool invisible
to the asker, a turn ending mid-sentence. An eval that bypasses the layer the defects live in cannot
find them, and would report green while production failed.

**Scoring splits on whether a signal can be argued with.** Everything decidable from the event
stream is decided there: which tools were called, whether a failure was surfaced, whether a cited
note id was ever returned by a tool. Only *"did this answer serve the asker"* goes to a model judge.
This is what turns "the model never called the tool that exists" from an opinion about prose into an
observation — the same discipline `tasks/lessons.md` records as the difference between measuring and
arguing.

**`expects_tools` is any-of, and `forbids_claims` is its mirror.** Demanding one specific tool would
grade the model's routing taste rather than the system's reach; several tools can legitimately serve
one question. The mirror matters more: `forbids_claims` names the assertions that would be
fabrication, so the *opposite* failure — claiming a capability the system does not have — is raised
mechanically even though settling it takes a judge.

**A probe carries the `bucket` we assigned before asking, and a bucket-C refusal is a pass.** `A` =
the capability exists, `B` = a substrate exists but the specific ask does not, `C` = nothing backs
it. The system genuinely cannot schedule an instrument or classify a mutagenic impurity; an answer
that says so plainly and offers what it can is *correct behaviour*, and grading it as a failure
would make the score a measurement of the tool list. The inverse — a fluent, well-formatted answer
to a question there is no data for — is the most serious defect the run can surface, so `fabricated`
is its own verdict that outranks the others rather than a low score folded into an average.

**The judge is a stronger model than the agent under test.** Grading is one call per probe against
the agent's many, so the quality is nearly free, and a judge sharing the agent's blind spots would
ratify them.

## Consequences

The behaviour eval is now a command (`python -m chemclaw.cli.live_probes`) over a versioned corpus
(`data/evals/probes/`), writing one transcript per probe so every finding cites evidence on disk
rather than a memory of a session. The deferred plan-vs-single-shot A/B becomes runnable for the
first time — it was blocked on precisely "the same tasks run twice against a live model".

The cost is honest: this eval needs a running deployment and a real model, so it cannot join
`make check`. It is a gate you run against a deployment, not a gate CI runs on a diff — which is
what AG-13 said it would have to be.

Two limits this decision does not remove. A judge is a model and can be wrong, so a `fabricated`
verdict claiming the system lacks a capability it actually has must be checked against the tool
inventory before it reaches a report. And a weaker agent model will miss tools a stronger one
reaches, so a tool-reach failure is ambiguous until the failing probes are re-run on a stronger
model — the run reports that re-run as its own class rather than blaming the system for the model.
