# D-2026-08-05-a-declaration-outliving-what-it-describes — A declaration outliving what it describes

**Status:** accepted · **Date:** 2026-08-05 · **Amends:**
D-2026-08-04-a-trade-off-has-no-single-best-point (the identity and the objective validator),
D-2026-08-04-a-limit-across-parameters-is-not-a-bound (the constraint identity),
D-2026-08-05-a-score-reported-more-precisely-than-it-repeats (the front tolerance)

## Context

Two independent reviewers went over the five BO waves after they merged. Both looked for the same
species of defect from different angles: **a declaration that no longer describes what it declares.**
Six were real. They are collected here because the fix is one idea applied six times — check the
statement against the code, not against the intent behind it.

## What was wrong

**1. A validator that can strand an in-flight campaign.** W3 added, to `OptimizationProblem`, an
unconditional refusal of an objective sharing a parameter's name. Nothing forbade that before, so a
campaign launched earlier may carry one — and this model's validators re-run wherever such data is
read back: `BoCampaignWorkflow` revalidates its `CampaignSpec` on **every replay**, and
`read_campaign_thread` revalidates the stored problem on every resume. Confirmed: a legacy payload
with a parameter and an objective both named `yield` now raises `ValidationError`. That campaign
would be permanently un-replayable and un-resumable, stranding every paid evaluation.

The discipline is documented three times in that file — for the round ceiling, for the objective
count — and was broken one validator below.

**2. Three feature spaces on one campaign id.** `_SPACE_FIELDS` excluded `descriptors` outright, on
the reasoning that they are computed *from* `structures`. That holds only when structures are set.
A caller may supply descriptors directly, and then they are the **only** statement of what the
surrogate sees. Confirmed: a bare categorical, one on `{A: 1, B: 2}` and one on `{A: 99, B: -99}`
all hashed to `campaign-7c795b04ad3df7e5`. Since `record_suggestion` upserts `problem`, one
caller's decision space overwrites another's on the shared row — precisely the "seeded with
observations from a different campaign" failure `read_campaign_thread` exists to prevent.

**3. One campaign forked in two by the order a sum was written in.** Confirmed:
`parameters: ["acid","base"]` and `["base","acid"]` — the same polytope — gave
`campaign-b021e1e4…` and `campaign-3eada623…`, each with an empty history. Same for an exclusion,
whose `forbids()` is symmetric in its two parameters while its hash was not.

**4. `read_only` on two tools that spend xTB and write rows.** `suggest_next_experiment` and
`predict_outcome` both featurize, and `featurize_problem` runs xTB per option and upserts into
`calculation_results`; the first also writes `bo_campaigns` and `bo_suggestions`. The manifest
field's own definition is "spends resources or writes data", and `agent/authz.py` records the
precedent: `compute_xtb_energy` is classified `state_changing` for spending exactly this, and is
named there as the live repro a narrower set would have missed. The comment beside the list said
`predict_outcome` "records nothing" — true of the campaign tables, false of the calculation cache.

**5. `assay_noise=0.0` reported as silence.** `if self.front_tolerance` read an explicit zero as
"none given", so a caller who said "compare exactly" was told no reproducibility was given and
invited to pass one.

**6. Two smaller ones.** `campaign.optimize` spent its entire budget before refusing a
multi-objective problem, from `best_of` *after* the loop, while its docstring promised the round
ceiling was "rejected here, before any budget is spent". And the fit-quality loop zipped objectives
against surrogates **by position**; `strict=True` catches a count mismatch, not an ordering one, so
a reordering would silently attach one objective's R² to another.

## Decision

**A rule that can reject stored data lives outside the model.** `require_names_do_not_clash` is
enforced where data *enters* — the four tool entry points and the campaign launch — and never in a
validator. Objective-name *uniqueness* stays in the model: every legacy payload carries exactly one
objective, so it cannot reject anything already written.

**Descriptors identify the space when, and only when, the caller stated them.** `_space_of` adds
them when `structures is None` **and** `descriptors is not None` — the second condition matters, and
its absence broke a pinned id on the first attempt: adding a `descriptors: null` key changed the
payload for every bare categorical. Added-only-when-informative is the same rule `objectives` and
`constraints` already follow.

**Constraints are canonicalized before hashing.** A linear form hashes its sorted
`(parameter, coefficient)` pairs, an exclusion its sorted `(parameter, sorted(options))` pairs, and
the list itself is sorted. Order the caller happened to write in cannot fork a campaign.

**`_IDENTIFYING_EXCLUSIONS` plus a test that the allowlist stays exhaustive.** An allowlist inverts
the denylist's failure rather than removing it: a field added to a parameter later is silently *not*
hashed, and two different spaces share one id. `test_the_identity_allowlist_still_covers_every_parameter_field`
asserts `_SPACE_FIELDS | _IDENTIFYING_EXCLUSIONS == ` the models' own fields, so a new field forces
an explicit decision instead of a silent one.

**Both featurizing tools become `state_changing`.** This gates them behind plan approval, which is
the intended consequence: the partition fails *open*, so a wrong "read" ships an ungated spend that
looks exactly like a gated one. `resume_campaign`, `generate_screening_design` and
`campaign_progress` stay `read_only` — none featurizes and none writes.

**`front_tolerance is None`, not truthiness.** Zero is a stated choice.

**The remaining two:** the objective-count refusal moves beside the round ceiling in
`campaign.optimize`, before any evaluation; and the fit loop matches surrogates by their own output
key, raising `SurrogateFitError` if an objective has none rather than mislabelling a score.

## Consequences

- The three pinned campaign ids are unchanged, and that is the load-bearing check on this whole ADR:
  `campaign-6958b7edaa261c83`, `campaign-55e5f929fe83a9a5`, `campaign-109f34eac28892ab`. A
  descriptor-carrying problem *does* get a new id — it has to, that is the fix — but only where
  descriptors were caller-supplied with no structures, which no real row has, since
  `featurize_problem` always sets structures first.
- `bo_campaigns.objective`/`direction` keep the lead objective only. No column was added because
  nothing queries by objective; the DDL comment now says so instead of claiming the id is "a hash of
  the decision space and the objective", which stopped being true when objectives became plural.
- Gating the two tools changes agent behaviour under an approval-first posture. That is a real
  change and it is the point of the classification; it is called out here rather than buried in a
  manifest diff.
