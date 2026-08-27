# D-2026-08-27-a-verdict-at-the-margin-is-a-coin-toss — the judge re-rolls inside a measured review band, and the median decides

## Status

Accepted. Closes the DEFERRED row "The judge's verdict is not reproducible at the threshold"
(deleted in this commit); its sibling row ("A flagged answer is never revised") stands untouched —
its blocker is a stronger model, not this.

## Context

D-2026-08-16's null control measured the defect: re-scoring 39 unchanged flagged answers cleared
5.1% of flags per roll, so a `review_required` flip could mean the judge rolled again rather than
that anything changed. It was a *margin* effect — answers scoring 1.00 scored 1.00 six times out
of six — but `verifier_confidence_threshold` (0.7) sits exactly where the margin is. The obvious
fixes were all blocked or bad: the judge model rejects an explicit temperature outright, and
best-of-n on every answer costs n× on the hot path. A hysteresis band was buildable offline, and
was deliberately not built, because its width would have been a magic number until somebody
re-rolled the judge and measured the spread.

## The measurement

`make live-verifier-margin` (`cli.verifier_margin`, shipped in this commit) re-rolls exactly the
call the turn makes — `verifier.judge_once`, extracted for this purpose: one structured roll, no
band, no degrade — 4× over 24 (answer, evidence) pairs built on the repository's own knowledge
notes in the three classes the 2026-08-16 live run actually flagged: grounded, drifting (one
ungrounded number) and contradicted. Run 2026-08-27, `claude-haiku-4-5` as judge, 96 rolls, 0
failed; the full artifact is `docs/archive/verifier-margin-2026-08-27.json` and the summary is:

| | value |
|---|---|
| pairs near the threshold (median within 0.25 of 0.7) | 8 — all of them the *drifting* class |
| flip rate per roll, near the threshold | **6.25%** |
| max deviation from a pair's median, near the threshold | **0.167** |
| grounded class (medians 1.0) | spread **0.000** over 32 rolls |
| contradicted class (medians ≤ 0.4) | spreads up to 0.25, all safely below the band |

Three things the numbers say. The **6.25% is the DEFERRED row's 5.1% reproduced on an independent
corpus** — same judge family, same margin effect, same order of magnitude — which is what licenses
using this corpus to size the band. The instability is **confined to the margin exactly as the
null control claimed**: the grounded class did not move once in 32 rolls, and the contradicted
class's spread happens 0.3+ below the threshold where no flip is possible. And the drifting class
— the one a chemist actually needs reviewed — lands *on* the threshold (one pair rolled
0.71/0.62/0.50/0.62 across it), which is why single-roll verdicts flip there.

**What this corpus is not**: the deployment's distribution of answers. It measures the judge's
per-answer stability (what a band width is made of), not how often real answers enter the band.
Re-fitting on a deployment's own flagged answers is the same command with `--pairs`.

## Decision

`verify_answer` re-rolls only at the margin: a first-roll confidence within
`verifier_review_band` of the threshold triggers up to `verifier_band_rerolls` (default 2) further
rolls of `judge_once`, and the roll with the **median confidence** becomes the verdict — its
claims travel with it, so the reported claims always belong to the reported score. Outside the
band the single roll stands, which confines the cost to the answers that need it: on this corpus,
the whole grounded and contradicted classes stay single-roll.

- `verifier_review_band` defaults to **0.2** — the measured 0.167 max deviation rounded up to the
  next 0.05, i.e. the width that absorbed every observed near-threshold roll. `0` switches the
  band off. Each reroll gets its own `verifier_timeout_seconds` budget.
- A **failed reroll is dropped**, and the median of the rolls in hand decides: one judged roll
  already exists, and the citation gate is a weaker verdict than the judge's own median-so-far.
  Only the *first* roll failing degrades to the deterministic gate, as before.
- `chemclaw_verifier_band_rerolls_total` counts every extra roll, so the band's real cost is a
  ratio on a dashboard rather than a belief.

## Alternatives rejected

- **Make the judge deterministic** — the judge model rejects an explicit temperature
  (`400 invalid_request_error`, measured 2026-08-16), and a per-profile temperature knob for a
  model that refuses it is a control that reads as one.
- **Best-of-n on every answer** — n× on the answer hot path to stabilise verdicts that are
  measurably already stable (32/32 identical rolls on the grounded class).
- **Sticky verdicts / true hysteresis** — needs a prior verdict per answer, and an answer is
  scored once; the band is the memoryless form of the same idea.

## Consequences

- Switching the verifier on now also means the startup capability probe (PR #247) and this band:
  the chart's commented opt-in block and runbook §(xvi-b) name both.
- Worst-case verified-answer latency inside the band is `(1 + rerolls) × verifier_timeout_seconds`
  after the answer already exists; the answer is never held hostage (unchanged).
- The DEFERRED row's other escape — "or make the judge deterministic" — stays available to a
  deployment whose judge accepts a temperature; the band composes with it (a deterministic judge
  simply never disagrees with itself, and the rerolls return identical values).
