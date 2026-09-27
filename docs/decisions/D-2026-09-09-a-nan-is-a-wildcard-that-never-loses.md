# D-2026-09-09-a-nan-is-a-wildcard-that-never-loses — the failed measurement, the flat assay, and four other places the BO bundle answered a question it could not answer

**Status:** accepted · **Date:** 2026-09-09 · **Builds on:**
D-2026-08-04-what-bofire-does-when-you-actually-run-it (the measurement register the multi-objective
front, the constraints and the cross-validated fit quality were all built from),
D-2026-09-05-the-gate-follows-behaviour-not-knowledge (the gate this bundle was still describing to
the model), D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution (the absence-test
shape) · **Beside:** `chemclaw.core.jsonb`, wave 11's shared `json_column` guard, which the campaign
store is one call site of · **Supersedes nothing**

## Context

Wave 11's review of `science/bo` and `connectors/bo` found seven defects. Six are fixed here and
one half of the seventh is deliberately not. Every one was reproduced before it was touched, and
each fix ships with a test that was watched failing first — which is how the second finding was
found at all, because its symptom is a number that *looks right*.

The severity order is not the code's order. **A wrong scientific answer outranks a crash**, and two
of these are wrong answers a chemist has no way to see through.

## Decision

### 1. A NaN in a non-lead objective was a wildcard that never lost, and took the Pareto front

`Observation.value` has carried `Field(allow_inf_nan=False)` since it was written, with a comment
giving the reason: *"NaN compares false in both directions, so it would silently win `best_of`"*.
`values` — the map holding **every other** objective on a multi-objective problem — was a bare
`dict[str, float]`. So the argument was made, written down, and applied to one field out of two.

`_dominates` asks two questions per objective: `gain < -tolerance` (worse here) and
`gain > tolerance` (better here). A NaN answers **False to both**, so the unmeasured axis reads as
"no difference", i.e. at least as good. Measured on two runs:

```
front: [(90.0, {'yield': 96.0, 'impurity': nan})]
clean (95% yield, 0.5% impurity) on front? False
failed (96% yield, impurity NOT MEASURED) on front? True
```

The chemist is shown a one-point trade-off consisting solely of the condition **whose assay
failed**, and the genuine 95%/0.5% run is deleted from the front. Nothing crashes and no number in
the output is wrong; the set is wrong.

**A failed measurement is an absent run, not a run with a NaN**, so the boundary says so:
`values` is now `dict[str, Annotated[float, Field(allow_inf_nan=False)]]`.

**And a belt in `observed_value`**, because `values` is a plain dict on a model that is neither
frozen nor validated on assignment — `observation.values[name] = nan` writes straight past the
field. `observed_value` is this module's own declared "single definition of this observation's
number for that objective", and all three readers come through it: the dominance test, the plateau
read, and the frame handed to BoFire (where a NaN is dropped mid-campaign rather than read as a
tie). One refusal there covers three call sites that each fail *differently*.

**Tightening a persisted field is normally refused here** — `require_names_do_not_clash` and
`core/jsonb`'s docstring both argue that it strands an in-flight campaign at replay. It does not
apply: a durable campaign is single-objective by `require_problem_yields_one_best_point`, so
`values` is empty on every payload that crosses the Temporal boundary, and a Postgres deployment
could never have persisted one (`jsonb` refuses NaN). There is no in-flight row this can fail.

### 2. An objective with no spread at all reported a perfect fit

R² is the fraction of the target's variance a model explains. With zero variance there is no
denominator, and the library answers 1.0. Measured, three times running on eight runs all reading
42:

```
n=8 all-42 run0: r2=1.0000 mae=0
summary: "Cross-validated on 6 run(s) over 5 folds, the surrogate for 'v' predicts held-out runs
          with R² 1.00 and mean absolute error 0."
```

`FitQuality` carries two caveats and **both are about precision** — small-sample instability, and
the GP hyperparameter fit's non-determinism — so neither fires on the case where the number is not
imprecise but undefined. A campaign whose assay has flatlined (dead catalyst, saturated response,
mis-plumbed detector) is exactly when a chemist asks whether the model is worth listening to.

Note what the code already believed: `SurrogateFitError`'s message names *"an objective with no
spread across the points seen so far"* as a cause of a **raised** failure. Measured, that input does
not raise. It scores 1.00.

`r2` is now `float | None`, `None` when the held-out target's spread is zero, and `summary` returns
a sentence about the *runs* instead of a score. `mae` stays a number: 0 over a constant target is a
true statement about the predictions, and it is the R² that overclaims.

### 3. The two campaign stores disagreed, and the durable one failed a call its docstring said cannot fail

`InMemoryCampaignStore` is a **deployment backend**, not a test double — `session_store="memory"`
selects it — and its docstring claimed *"Every rule its Postgres sibling enforces holds here in the
same terms"*. From one input:

```
InMemory: record() SUCCEEDED -> id=1 created=True
Postgres: record() RAISED ValueError: Out of range float values are not JSON compliant
```

`_TRANSIENT_WRITE_FAILURES` is `(ConnectionError, OSError, TimeoutError, psycopg.Error)` and does
not include `ValueError`, deliberately — the comment beside it says a non-finite float is *our*
defect and must not be swallowed. That is right, and it means the refusal escapes
`record_suggestion` and fails `suggest_next_experiment` **after** the candidates have been computed:
the one outcome that function's contract exists to prevent. A dev stack recorded the campaign and a
deployment lost the chemist's suggestion.

Neither suite could see it. `tests/test_bo_campaign_record.py` drives the in-memory backend and
`tests/test_postgres_campaign_store.py` drives Postgres, so each half tested its own behaviour and
nothing tested that they are the same behaviour.

Three parts: the module drops `_json`/`_STRICT_JSON` for `core.jsonb.json_column`, so the rule has
one definition; `InMemoryCampaignStore.record` applies the same refusal through `STRICT_JSON`
(`json_column` alone cannot — `Jsonb(value, dumps=...)` is lazy and only serializes at the wall);
and one scenario list runs against both backends, which is the only shape that can catch a
divergence rather than each side's own behaviour.

**The refusal is not moved into `_TRANSIENT_WRITE_FAILURES`.** A NaN in a payload is still this
code's defect, and swallowing it would make a deployment where every BO write fails
indistinguishable from one where none do — the argument that comment already makes. What changes is
that both backends now fail on the same inputs, so the defect is visible in the dev stack that is
cheap to run rather than only in the deployment that is not.

### 4. Contradictory constraints were diagnosed as a data problem, on a path with no data

`x1+x2 <= 1` beside `x1+x2 >= 4` was refused — correctly — and the advice was wrong twice:

> This is usually duplicate or near-duplicate observations collapsing the model's kernel, or an
> objective with no spread — vary the inputs, or the measured values, before retrying

It reaches this from the **seeding** path, where a `RandomStrategy` runs and there is no surrogate,
over **zero** observations. So it names a model that does not exist and offers an action the caller
cannot take, and a model handed that sentence retries with different numbers against a polytope
that is still empty.

`_translating_surrogate_errors` now matches botorch's `InfeasibilityError` **first**, and names the
constraints in the chemist's own relation via `describe()`. Matched on the *type*, not on the
message — a substring read is how the generic advice came to be attached to this case, and
`tests/test_bo_constraints.py` pins the type so a bump that removes or re-parents it fails loudly
rather than silently reverting the diagnosis.

It stays in `_BAD_DATA_TYPES` and stays non-retryable, which is right for the same reason it is
right there: the same spec fails the same way. `SurrogateFitError`'s docstring now says *input*
rather than *data*, because calling this bad **data** is precisely what produced the sentence above.

### 5. The bundle was still telling the model the recommendation is PR-gated

`connector.yaml`'s job description said `start_optimization_campaign` *"opens its recommendation as
a PR-gated note for human review"*. **That text is the tool description the model reads on every
turn.** `D-2026-09-05-the-gate-follows-behaviour-not-knowledge` removed the gate and deleted
`kg/proposal.py`; `ConnectorJobWorkflow` writes the note straight into the graph with
`created_by: agent`. So the agent told a chemist a person would check their recommendation before
it landed, and it landed immediately — a claim about a control that does not exist, in the direction
that overstates safety.

Corrected in the manifest and in the four docstrings that restated it (`knowledge.py`,
`workflows.py` twice, `activities.py`, plus one in `science/bo/problem.py`), and held by an
**absence test** in the shape `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`
established: whoever re-adds the claim has to add the producer too. The forbidden phrases are narrow
on purpose — *"these are proposals a human runs"* is true, and the skill says it at length; what is
forbidden is the claim that a reviewer stands between the note and the graph.

### 6. `count` was the one model-supplied size in this bundle with nothing above it

Every sibling is bounded by config — `bo_max_design_runs`, `bo_max_evaluations`,
`bo_max_enumerated_cells`, `bo_max_rounds`. `suggest_next_experiment(count: int = 1)` was not.
Acquisition cost is linear in the batch: measured 1.3 s at 2 candidates and 2.6 s at 4 on an
unconstrained two-parameter problem (~0.65 s each), and the tool's own docstring puts a
*constrained* problem at roughly nine seconds per further candidate. Behind the bundle's
`request_timeout: 120`, a three-digit `count` is a request the client abandons while the pod keeps
computing it.

`bo_max_candidates_per_ask` defaults to **96** — a plate, the largest batch anybody runs at once —
and is enforced in `propose_candidates`/`initial_candidates` rather than in the tool signature, for
the reason `_require_design_fits_the_ceiling` already states: *a bound in the transport is a bound
the in-process callers do not get*. The durable campaign's per-round `batch` reaches those two
functions without passing through a tool schema at all, so it is held to the same number.

**It is deliberately not a latency guarantee**, and saying so is the point: 96 constrained
candidates would still outlast that 120 s timeout. The number that bounds latency is the
transport's. This bounds the ask.

### 7. A campaign over a space its objective cannot read is refused at launch — the ranges are not

`require_campaign_startable` accepted a spec naming `reizman_suzuki` over an unrelated decision
space. The failure arrived at *evaluate* time as `KeyError: 'catalyst'` — hours into a durable run,
after the seed rounds had been paid for, and as an exception neither `SurrogateFitError` nor
`_BAD_DATA_TYPES` matches.

This is the same shape as the direction mismatch `RegisteredObjective.direction` was added for: what
a registered objective *is* — a function over named parameters, better in one direction — is a
property of the objective, and the registry is where it is stated. `requires` joins `direction`
there, and `require_problem_supplies_what_the_objective_reads` is the fourth launch rule.

**The ranges half is left alone, deliberately.** A spec may widen a continuous bound past the
benchmark's training data, and the fitted emulator extrapolates flat:

```
T=110 -> 92.19    T=300 -> 92.19    T=1000 -> 92.19
```

Three reasons not to guard it here. It is a property of *every* fitted emulator rather than of this
one, so a check written against `reizman_suzuki`'s bounds would be a check about one registry row.
The inline path already answers this question honestly and generically — `Prediction.in_domain` says
a point is outside the declared range and widens the sd. And a launch-time range check needs the
registry to carry a decision space, which is a second optional field used by one of two entries —
the abstraction-with-one-caller this repository inlines rather than builds. It is recorded here
rather than half-guarded, and it stays latent: nothing in `src/` drives the benchmark that way.

## Consequences

- **Two of these are wrong scientific answers rather than crashes**, and both were reachable from an
  ordinary chemist question. A NaN in a secondary assay is not adversarial input; it is what a
  failed HPLC injection looks like on the wire.
- `FitQuality.r2` is now nullable. Anything reading it must handle `None`; in `src/` only `summary`
  did, and it branches first.
- `Observation.values` is stricter at the boundary. A caller that was sending a NaN gets a
  `ValidationError` naming the objective instead of a silently corrupted front.
- The BO bundle is one of five call sites `core/jsonb` was hoisted for, and the only one where the
  two backends had visibly diverged; the differential test is what keeps them together, not the
  docstring that claimed they already were.
- **What the fifth finding is really about**: the same removal that deleted a control left five
  present-tense sentences describing it, one of them model-facing. `D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit`
  is usually read as being about *numbers*; a claim about a **control** rots the same way and is
  worse, because a stale number misleads a maintainer and a stale control claim misleads a chemist.
