# D-2026-09-06-a-ratio-measured-over-a-request-belongs-to-the-whole-request — 140,500 billed against a 119,000 budget

**Context.** `D-2026-08-28-a-budget-in-the-wrong-unit-is-not-a-budget` made
`agent_context_token_budget` a **billed**-token budget, converted into the estimator's unit by a
ratio measured from the provider's own `input_tokens`. `D-2026-09-04-a-budget-that-excludes-the-
prefix-is-not-a-budget` then charged the request's prefix against it unconditionally.
`D-2026-09-05-a-ratchet-that-binds-no-connectors-measures-a-smaller-system` corrected the prefix
those two are derived from. All three are revisited here on purpose.

`effective_trigger` computed `(budget - prefix) / ratio`: subtract an *estimated* prefix from a
*billed* budget, then convert only the remainder. Its own docstring defended that split as
"deliberate rather than sloppy", on the ground that the prefix is the content where the two units
agree — a ground nothing had measured on this prefix.

**Measured.** `note_model_call` computes `billed / estimated` over the **whole** request. Spending
that on the remainder converts to `prefix * 1 + thread * ratio <= budget`, which holds only if the
prefix bills at exactly one billed token per estimated one. It does not. Measured 2026-09-06 over
the observed `default` prefix against real BPE encodings it bills **0.985** (`o200k_base` and
`cl100k_base`; 1.0538 on `p50k_base`), and results called from `Chemclaw3-mcp`'s own `chem` server
bill **1.24x to 1.67x**. The prefix's over-estimate is credited against the thread's
under-estimate, and the thread is then permitted to grow into the credit.

Driven on a compiled graph with all **113** tools a shipped turn binds, shipped defaults, a 128k
window declared (the best case, and the value the chart states), 40 turns, and a thread of real
`chem.enumerate_bond_cleavages` results tokenized by `o200k_base`: the converged request billed
**133,803** — 14,803 over the 119,000 budget and 9,899 over the 123,904 a 128k model accepts — with
`chemclaw_context_unreducible_total` flat on all forty. On the in-repo surface with a
word/punctuation meter the same shape gives **140,500**. Prose threads bill 116,883 and are fine,
which is why every existing test missed it: the payload class that breaks it is the one the
triggers exist for.

Two more from the same measurement. `agent_context_calibration_min_calls` shipped at **20**, so
every pod's first twenty model calls ran at ratio 1.0 and billed **164,989** on that arm — the
largest overrun this subsystem produces, on every restart and every scale-up. And the EWMA was
seeded at 1.0 with `_ALPHA = 0.1`, so it is mostly its seed for ~20 samples: lowering the floor
alone moves calls-over-ceiling from 20 to **19**.

**Decision.**

1. *The budget is converted whole.* `trigger = budget / ratio - prefix`. Then
   `billed = ratio * (prefix + thread) <= budget`, with no assumption about how the two populations
   differ: the ratio is applied to exactly the quantity it was measured over. **This can only
   tighten** — `budget/ratio - prefix <= (budget - prefix)/ratio` for every `ratio >= 1`, and the
   clamp guarantees that — and at `ratio == 1.0` the two are the same number, which is why every
   unit test of this function passed either way. Same arm: **133,803 → 118,589**.

2. *The two-population model was built and is not shipped, with the measurement that decided it.*
   `prefix * r_p + thread * r_t <= budget`, with `r_t` measured directly as
   `(billed - prefix) / thread`, is the structurally correct model and is what the wave brief
   asked to be considered. Driven on the same fixture it reaches the fixed point on turn **1**
   instead of turn 5 — and reaches **the same fixed point**: both arms converge to a thread of
   22,721 estimated tokens and a request billed 119,089, because the whole-request ratio
   self-consistently adjusts to the composition the trigger permits. It is declined for now on
   cost: it changes what `estimator_ratio()` *means* from a request ratio to a thread ratio, and
   `agent/turn_usage.py::unbilled_tokens` multiplies a whole-request estimate by it — a file this
   change may not touch and a silent 60% over-book if it did. Keeping two averages to keep both
   meanings is the shape a second caller would justify; today there is one. The faster warm-up is
   the trigger for revisiting.

3. *The first sample is believed, and it is the sample.* `agent_context_calibration_min_calls`
   defaults to **1**, and `_Calibration.ratio` divides the seed back out
   (`(ewma - (1-a)^n) / (1 - (1-a)^n)`). The floor could only ever gate the *safe* direction —
   the ratio is clamped at 1.0 below, so believing a sample can only tighten — and its real effect
   was to hold the loose end for twenty calls. Calls over the 128k input ceiling on that arm:
   **20** as shipped, 19 with the floor alone at 1, **1** with both. The one that remains is the
   process's very first call, before any sample exists, which no policy can bound.

4. *The bound from above loses its tokenizer constant.* `WORST_PREFIX_ESTIMATOR_RATIO = 1.0534` was
   a hand-transcribed literal (measured today: **1.0538**) whose only reason to exist was the unit
   mixing in (1). With the conversion whole, a calibrated maximal request bills the budget and the
   upper bound is `budget <= window - llm_max_tokens` — 119,000 against 123,904, no encoding in it.
   What that cannot bound is named in the test rather than implied: the uncalibrated first call.

**What it costs, because it is a behavioural change.** A calibrated pod's thread allowance falls.
At the shipped defaults and an evidence-traffic ratio of 1.208 the window edit leaves **22,761**
estimated tokens of thread where the old arithmetic left ~37,000 — but those requests were over
budget, so the larger number was never a thread allowance, it was an overrun. The lossless edit
falls to ~12,000, which is the early-and-often behaviour it is for. `tests/test_compaction.py` now
asserts the warm state, and the floor it asserts is the one with a consequence: the window's
allowance must exceed `agent_max_tool_result_chars / 4`, the newest tool batch neither edit may
reclaim, below which every evidence turn is unreducible. Today that is 22,761 against 15,000.

**The bound is a near-identity, and the residue is measured rather than assumed away.** The
conversion uses the ratio as it stands *before* the call it is about, so a request is budgeted
against a slightly stale average of a sequence the trigger itself steers. Traced over 30 turns the
fixed point is a two-state cycle at 115,800 and **119,089** — 0.075% over — and
`tests/test_compaction._TRACKING_SLACK` holds it at 1%, 13x the observation, while the criterion
that matters clears by 4,815.

**Consequences for the tests, which is where the defect was allowed to live.**

- `tests/test_context_budget.py`'s soundness sweep asserted `prefix + sent * ratio <= budget` — the
  same asymmetry as the code, so the two agreed with each other at every point. Written whole it is
  red against the old arithmetic at every point with `ratio > 1` and `prefix > 0`; watched fail.
- `tests/test_compaction.py` built its end-to-end graphs **without `connectors=`**, measuring
  43,497 against the 64,586 of in-repo surface a turn binds — the defect
  `D-2026-09-05-a-ratchet-that-binds-no-connectors-measures-a-smaller-system` closed one file over,
  five days later. Every graph budgeted through `_request_budget` now binds the same surface the
  prefix was measured on, and the guard that would have caught it names the tools rather than
  bounding the prefix's size (the old `prefix > 0.3 * budget` passes at both numbers).
- No end-to-end test in that file had a model that reports `usage_metadata`, so every one ran
  permanently uncalibrated — at the one ratio where the two arithmetics agree.
  `test_a_calibrated_process_does_not_bill_past_its_budget` is that gap closed, with a deterministic
  network-free meter so the shipped budget's bound is not skippable by an egress rule.

**Six present-tense figures corrected, and the doctrine applied rather than restated.** The module
docstring carried 43,175 as the prefix; `effective_trigger`'s carried "eight connector bundles"
(there are **seven**) and 75,695; both it and `core/config/agent.py` repeated the "1.04x" that the
same file's own paragraph says it was correcting; two request totals predated the prefix being
charged. Each is replaced by the test that measures it, not by a fresher number. The `0.45x`
connector-JSON figure — 2.2x billed per estimated, the sole justification for
`agent_context_calibration_max_factor = 4.0` — **does not reproduce**: the direction holds and the
magnitude was 33-45% high, and the ceiling now rests on a measured 1.67x worst case. The
`~100 ms`/`~20 ms` schema-sweep timings are deleted rather than corrected (16 ms / 0.01 ms today,
under conditions the originals never stated), leaving the counts the tests actually assert.
