# D-2026-08-27-a-number-with-no-name-is-not-a-measurement — `values` and `result_inline` on the tool-result event

## Status

Accepted. Two additive fields on `ToolResultEvent`, one new extractor, one new setting. Both come
from a frontend design study that measured what its surfaces could and could not say, and both are
the *service* side of a compromise the browser was making honestly and badly.

## Context

`ToolResultEvent` already carries four views of one result, and the reasoning for each is in its
docstring: `preview` for a human, `note_ids` and `numbers` for a grounding check, `result_ref` for a
surface that decides to render one result in full. That set was assembled to answer "was this in
front of the model?", and it answers it well. It was then asked a second question — "show a chemist
what came back" — and two gaps appeared that no amount of frontend work can close.

**A bare number cannot be displayed.** `numbers` is a positional list of floats with nothing saying
what any of them is, because the check it feeds does not care: it asks whether a figure an answer
states was returned by *some* tool this turn, and a label would be noise. But the UI's entity rail
joins values to the structure they were computed for, and the most it could ever truthfully write
was `predict_pka returned 4.76, 1.6`. Its own source says why it stops there: "dressing it up as
`pKa = 4.76 ± 1.6` would invent an order the wire does not promise and a meaning it does not
carry". That is the correct call, and it leaves a calibrated uncertainty rendered as an anonymous
second number.

**A small result cannot be rendered without a round trip.** The preview/ref split exists because a
40-chunk evidence sweep must not be streamed to a browser on every turn — right, and it is applied
to every result regardless of size. Measured on this repository's own recorded results: an
`ich_impurity_limit` answer is a few hundred bytes and a `predict_pka` answer is under a hundred.
Each pays a second HTTP round trip to be rendered as anything but prose, for a payload several
times *smaller* than the 200-character preview that is already on the wire. The frontend's
concession was to fetch lazily and cap the number of blocks it renders per turn — a real cost paid
for a rule that was never about results this size.

## Decision

**`values: list[ResultValue]`** — the same numbers as `numbers`, each under the key path the tool
filed it under, with the unit the payload states beside it or nothing.
`chemclaw.core.quantities.labelled_values` walks *parsed JSON* and refuses everything else:

- the label is the payload's own key path (`pka`, `limit.limits.0.value`), never prettified,
  never mapped through a table of nicer names;
- the unit comes only from a `unit`/`units` string in the same object — the
  `{"basis": …, "value": 0.5, "unit": "µg/day"}` shape the ICH tables use — because reading one
  from a parent or a sibling would be a guess about which numbers it applies to;
- a result that is not JSON yields nothing, and does not fall back on the number grammar with a
  label guessed from the preceding word. The figures are still on the wire in `numbers`; they
  arrive unnamed, which is what they are.

`numbers` is unchanged, and the two coexist for the reason `note_ids` coexists with `preview`: a
grounding check wants every value and no names, a value strip wants names and must not guess them.

**`result_inline: str`** — the result itself when it is under `stream_inline_result_bytes`
(4 KiB, 0 disables), empty when it is over. The cap is the control, and it sits two orders of
magnitude below `stream_max_result_bytes` deliberately: this is a shortcut for the small results,
and the number is what stops it becoming the path a sweep takes to a browser every turn. Nothing
else changes — `result_ref` is still minted, the store is still the record, and an over-cap result
reaches a surface exactly as it did.

## Consequences

- A surface can write `pKa 4.76` and, where the tool said so, `0.5 µg/day` — and still cannot write
  `4.76 ± 1.6`, because nothing on the wire says the second number is an uncertainty on the first.
  That remains a question for whoever owns the tool's return shape.
- A small result renders with the turn, with no second request. `Chemclaw3_ui`'s result blocks fall
  back to the ref exactly as before when the field is empty, so the behaviour degrades to the
  current one rather than breaking.
- Both fields are additive with empty defaults: an existing consumer is unaffected, and an older
  service simply sends neither.
- `tests/test_event_contract.py` fired, which is what it is for. The fixture is regenerated in this
  commit and `Chemclaw3_ui`'s `shared/events.ts` mirror — the interface *and* `normalizeEvent`,
  which rebuilds every event field by field — is updated in the same change.
- The third ask from the same study is **not** taken here: a duration on a *rehydrated* transcript
  would need `history.get_messages` to carry each row's `created_at`, which widens the provider
  contract shared with the in-memory store (which has no timestamps at all) for one rendering. The
  marker that ask was ostensibly about already exists — `TranscriptToolCall.result is None` is
  "ran, ending unknown", and the UI renders it as such. Left open deliberately.
