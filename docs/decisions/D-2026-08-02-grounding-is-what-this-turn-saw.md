# D-2026-08-02-grounding-is-what-this-turn-saw — Grounding is what this turn saw

**Status:** accepted · **Date:** 2026-08-02 · **Extends:** D-032 (the low-confidence hold),
F10-B (the answer verifier), D-2026-08-02-a-probe-is-a-question-you-have-not-asked-yet ·
**Relates to:** KM-9 (one shared corpus)

## Context

The live run measured fabrication at **22%** on stories the system can serve, **39%** where the
substrate is partial and **46%** where nothing backs the question. Monotonic: *the system is least
honest exactly where it knows least*. Three mechanisms that should have caught this were each
subtly wrong, and each was wrong in the same direction — reporting confidence it had not earned.

**The verifier checked the wrong thing.** `agent/verifier.py` resolved an answer's `[[wikilink]]`
citations from **the graph on disk**. So the question it asked was "does this note id exist?" when
the question a grounding check must ask is "did this turn see it?". A note id recalled from
training resolves perfectly well on a graph that contains the note — the check could not fail the
case it existed for. Worse, its deterministic backend scored an **uncited** answer at
`supported=True, confidence=1.0`. In the run, **0 of 33 analytical answers carried a single
wikilink**: every fabricated method in that slice would have scored a perfect citation-faithfulness
result. By the system's own measure an answer was safest when it cited nothing.

**Prompting was measured insufficient.** The capability-boundary paragraph added to `_INSTRUCTIONS`
before the run is necessary and it worked *partially*: invented parameter classes fell from 9 to 1
across the six worst probes. It did not change the **shape** of the answer, and the stronger model
produced a complete branded HPLC method table in the same reply as the sentence "not a validated
method".

**A whole layer's outage was never announced.** `api/runner.py` announced unreachable *connectors*
before the turn planned — its own docstring states the principle: *"the model cannot tell the
chemist that a tool was missing, because it never saw one missing."* Temporal was never probed. 0
of 7 durable launchers ran in the live run, and the model repeatedly read the launch failure as bad
input and re-asked the chemist for parameters it already had.

**And one control was invented outright.** Both models described a per-team read boundary that does
not exist. `authorize_tool` gates **tool names**; `_eligible_notes` filters only on arguments the
model itself supplies; there is one shared corpus. Telling a chemist their colleague's data is
being withheld, when it is not, is worse than an invented number: an invented *number* gets
checked, an invented *control* gets believed and designed around.

## Decision

**Evidence for a conversational turn is that turn's tool results, never the graph.** The runner
keeps every tool result in full (`_ToolCallTrace.outputs`) and threads it into the answer check.
The 200-character `ToolResultEvent` preview stays what it is — a UI budget — and is explicitly not
reused for grounding: a `gather_evidence` result is ~20,000 characters over 40 chunks, so scoring
against the preview would call 39 of its 40 citations fabricated.

**The eval harness does not yet agree, and saying it did was wrong.** `chemclaw.evals.live` scores
against the turn's tool results rather than the graph — that much it has always done, and it states
the reason in a comment — but it reads them off the SSE `ToolResultEvent`, which carries the
200-character preview. So the production gate now reads full text and the harness reads a
truncation, which makes `uncited_note_ids` a systematic **over**-count of fabricated citations. It
is also the only metric that could validate this change, so the gap is on the backlog rather than
buried here. Closing it wants an untruncated `note_ids` field on the event, not a bigger preview:
that budget is right for the UI.

**An uncited factual answer is unverified, not supported.** This over-flags: a purely
conversational "which batch do you mean?" is indistinguishable from an uncited assertion without
parsing claims, and gets flagged too. That asymmetry is chosen, not tolerated — over-flagging a
clarifying question costs a reviewer one glance; under-flagging an uncited HPLC method table is the
failure the gate exists for. An empty answer is the single exception.

**A deterministic shape gate sits beside the citation gate, config-gated and off by default.**
`ungrounded_parameter_shapes` scans the finished answer for the shapes a chemist reads as
specification — flow rate, gradient %B, wavelength, back pressure, column brand, µg/day, ppm,
polymorph form — and flags any class the turn's tools never produced. It is a **shape** heuristic
and the docstring says so in both directions: it misses a fabricated temperature or catalyst
loading, and it over-fires on a chemist's own 254 nm quoted back. It fires per shape *class*, not
per value, because comparing values would flag every answer that rounds or reformats a retrieved
number. Off by default because an answer marked for review that did not need it costs trust in
every mark after it — enabling it is a deployment decision, and it is honest about being a filter
rather than a proof.

**The durable subsystem is announced like a connector.** It rides in `CapabilityDegradedEvent`'s
existing list under the name `durable-jobs (Temporal)` rather than getting an event type of its
own: what a surface does with the name is identical, and a second contract for one more unreachable
capability would carry no additional meaning.

**The access boundary is stated, not implied.** Two paragraphs in `_INSTRUCTIONS` say what role
gates actually do and forbid describing a records boundary that does not exist.

## Consequences

- All five §17 governance stories had the same shape — *the mechanism exists and is not binding*.
  17.1 and 17.5 are now binding when their knobs are on; 17.4's gap is closed by telling the truth
  rather than by building a mirror.
- **This has not been re-measured live.** The gate is argued from the run that motivated it, not
  from a run that includes it. Until roughly six to thirty probes are re-run with
  `answer_shape_gate_enabled=true`, the honest claim is "the mechanism exists and its default is
  off", and `docs/archive/live-user-stories-2026-08.md` says so.
- `verify_answer`'s `degraded` flag is gone. It existed because an uncited answer scored 1.0, so an
  unreachable judge produced *more* confidence than a working one. With uncited meaning unverified
  for everyone, the degraded case and the ordinary case want the same verdict.
- Building the LLM judge client moved **inside** the try. It was outside, so a deployment that
  flipped `verifier_enabled` without a reachable `"verifier"` route got *no* verification instead
  of the documented offline fallback — the likelier of the two failures on the day the feature is
  switched on.
- `kg/analytics.py`'s `projects_without_distillation` is renamed to what it measures. It counts
  free-text topic tags (`playbook`, `solvent`, `suzuki`), and the correctly-computed field with the
  misleading name is what produced a fabricated portfolio status report.
