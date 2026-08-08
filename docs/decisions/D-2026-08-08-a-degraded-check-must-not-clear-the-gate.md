# D-2026-08-08-a-degraded-check-must-not-clear-the-gate — the substitute was more generous

**Status:** accepted

## Context

Four ways this system reported that everything was fine while it was not measuring anything.

**1. A judge outage scored a fabricated answer 1.0 and cleared review.** `verify_answer` degrades to
`_deterministic_result` when the LLM judge fails, which is the right move — the alternative is an
unscored answer. But the two checks answer different questions. The judge scores *faithfulness*: does
the answer say what the evidence says. The citation gate scores *resolvability*: do the wikilinks
name chunks this turn retrieved. Measured on the same answer and evidence, with a cited claim the
evidence contradicts:

```
judge up   -> confidence 0.0, supported False, review_required True
judge down -> confidence 1.0, supported True,  review_required False
fields differing between the two results: set()
```

The broken verifier read **stronger** than the working one, on exactly the answers a judge exists to
catch, and nothing on the result said which check had run. `verify_answer`'s docstring argued a
`degraded` flag was no longer needed because "an uncited answer is unverified now whoever asks" —
true, and it covers only the *uncited* branch. The cited-but-wrong case is the judge's whole purpose.

**2. Zero token metering silently disarmed the budget guard.** `usage_tokens` is duck-typed on MAF's
`UsageDetails` keys, which is the right shape — a provider reporting nothing must meter 0 rather than
fail a turn — but it makes an upstream rename indistinguishable from silence. With the keys renamed
to `input_tokens`/`output_tokens` it returns `TurnUsage(0,0,0,0,0)`; with `budget_enabled=true` (what
the chart ships) **50 turns of 15,000 real tokens each were booked as zero and `check()` still
allowed the next one**. `chemclaw_tokens_total` stays flat while the turn counter climbs, and
`turn_costs` fills with all-zero rows: a deployment that looks free and is not.

**3. A Temporal outage was detected every turn and reported to no operator.** The probe logs at DEBUG
on the stated grounds that "`open_reachable` already logs and counts a degraded turn". That comment
is checkable and false — the counter is `chemclaw_connectors_unreachable_total`, which reads
`tool.is_connected` over *connector* tools and never names Temporal. Measured at the shipped
`log_level=INFO` with the broker on a dead port: probe False, **zero log lines**, `METRICS.render()`
unchanged. Every chemist was told durable jobs were unavailable; the dashboard read healthy.

**4. Retrieved content could close the judge's evidence block.** `_verifier_prompt` wrapped evidence
in a hand-rolled `<evidence>` tag — not nonce'd, not defanged — so any retrieved or uploaded text
containing `</evidence>` escaped it and the rest landed at top level in the prompt that decides
`confidence` and `review_required`. The module's docstring deferred this until "a source carrying
such text lands"; `framing.py` already names attachments as one, and D-2026-08-06 indexes a mounted
share's documents as cited evidence. It had landed.

## Decision

**A check that did not run must not be able to clear the gate that check exists to guard.**

- `VerificationResult` gains `verified_by: "judge" | "citation-gate"`, and `AnswerEvent` carries it
  to the surface. The judge does not author the field — which check ran is a property of the call
  site, and a model that emitted it would be certifying its own reliability.
- A verdict not produced by the judge sets `review_required`, and a verification that *crashed* now
  sets it too with an explicit `"verification did not run"` claim, rather than emitting the same
  event a clean verdict produces.
- `usage_tokens` counts usage contents that carried no readable total. A turn with any of them logs
  at ERROR and increments `chemclaw_usage_unreadable_total`.
- The durable probe increments `chemclaw_durable_unreachable_total`, and the false comment is gone.
- The judge's evidence goes through `framing.frame_untrusted` — the same nonce'd envelope the
  conversation prompt uses.

**On the operational cost, which is real.** A judge outage now routes every answer to human review.
That is a deliberate trade and the honest one: the alternative is a deployment that keeps answering
with maximum confidence while the check that earns that confidence is down. `verified_by` is on the
wire so a surface can distinguish "degraded" from "genuinely low confidence" and say so, rather than
presenting an unexplained flag — the flag is the safety property, the field is the transparency.

**On what `unreadable` measures.** Not "the provider reported nothing", which is legitimate and
counts nothing — the fake agent in tests carries no usage content at all and is unaffected. It counts
a usage content that was *present and unparseable*, which is exactly the shape a key rename takes and
is detectable without knowing what the keys will be called next.

**On the evidence ids.** `frame_untrusted` sanitises an id to `[A-Za-z0-9._:-]`, correctly, since an
attribute is a place a value could break out of. That would collapse the space-separated id list this
block has always carried into one underscore-joined pseudo-id, while the judge is asked to return
"the id of the evidence note it relies on". So the list is named in a line *we* author ahead of the
envelope, and the envelope carries a single clean id.

## Consequences

Three counters that did not exist now cover three failure classes that were invisible:
verifier degradation, unreadable metering, and broker reachability. All three are the same shape —
a working-as-designed fallback with no signal — and the campaign's inventory found 23 more like them.

`test_the_same_citation_is_supported_when_a_tool_in_the_turn_returned_it` and its sibling changed
from asserting `review_required is False` to `True`. That is not a weakened test: their helper is
named `_offline_verification` and its own docstring says "verification on, judge unreachable", so
they were pinning the degraded path's *old* verdict. What they were written to prove — the citation
resolved, confidence 1.0 — they still prove.

What this does not do is make the citation gate stronger. It measures what it measures, and it is
right about that; the defect was that nothing recorded which measurement had been taken.
