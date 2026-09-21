---
name: system-suitability
description: >-
  Use when chromatography numbers are on the table — "is this method OK", "the RSD was 1.8%",
  tailing, resolution, plate count, "can I change the flow rate without revalidating", an
  injection sequence against its criteria. Reads USP <621> arithmetic over numbers a chromatogram
  already reported, and is clear about the two things passing suitability does not mean.
tools:
  - replicate_precision
  - plate_count
  - peak_symmetry
  - peak_resolution
  - retention_factor
  - permitted_method_adjustment
  - system_suitability_report
  - check_against_specification
  - ask_clarifying_question
---

# System suitability

## What a pass means, and the two things it does not

Passing system suitability says **the system performed at the moment of that run**. It is not
method validation, and it is not evidence that the result is accurate. Both mistakes are easy to
make in a sentence and expensive downstream, so state the scope when you report a pass.

A failure is the more useful signal and should lead the answer: a tailing factor that moved, a
plate count that halved, a resolution that closed up are each a *diagnosis*, not a number to log.

## Nothing here opens a chromatogram

Every input is a retention time, a width, an area or a count that a person or a data system already
reported. This bundle does not integrate, does not find peaks and does not assign a baseline. So
when a chemist pastes a chromatogram image or describes a trace, the missing step is a human
reading numbers off it — ask (`ask_clarifying_question`) rather than estimating from a description.

## Which tool answers which question

| The ask | Tool |
| --- | --- |
| "our replicate injections gave these areas" | `replicate_precision` — and it applies the compendial rule for how many injections that RSD limit needs, which is the part people omit |
| "is the column still good" | `plate_count` |
| "the peak looks skewed" | `peak_symmetry` — the tailing factor at 5% height |
| "are these two peaks separated" | `peak_resolution` — under both width conventions <621> defines, and they do not agree |
| "is the impurity eluting too early" | `retention_factor` |
| "can I change this without revalidating" | `permitted_method_adjustment` — isocratic only |
| a whole injection sequence against declared criteria | `system_suitability_report` |

**Use `system_suitability_report` for a table rather than decomposing it across the single-peak
tools.** Decomposing means pairing widths with retention times by hand and deciding which pairs are
*adjacent* for resolution — and a resolution computed between the wrong two peaks looks exactly
like a correct one. That is why the composite exists and why it is the most expensive schema in the
fleet.

## Resolution has two conventions and they give different numbers

<621> defines resolution at half-height and at tangent-baseline width. Say which one you used.
Quoting a half-height resolution against a criterion written for the baseline convention is a pass
that is not one.

## Adjustment is bounded and isocratic

`permitted_method_adjustment` answers whether a proposed change stays inside what the general
chapter permits without revalidation. Two limits to repeat every time: it covers isocratic methods,
and "permitted" is the compendial allowance, not your site's change-control process. A change
inside <621> may still need a deviation on paper.

Never devise the method parameters themselves. This system holds no column, gradient, flow rate,
wavelength or retention time of its own, and inventing one — even a plausible one — is the failure
the agent's prose contract denies in exactly these words.

## Joining up with the result

A suitability verdict is about the instrument; `check_against_specification` is about the material.
Keep them apart in the reply, and note the order that matters: a result from a sequence that failed
suitability is not a result yet, whatever the specification says about it.
