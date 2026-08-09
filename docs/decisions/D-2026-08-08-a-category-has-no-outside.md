# D-2026-08-08-a-category-has-no-outside — the two BO tool-surface defects an audit found, and the eight it refuted

**Status:** accepted · **Scope:** `connectors/bo/server/tools.py`

## Context

Bayesian optimization was the largest subsystem the 2026-08-08 review campaign never opened:
`science/bo/engine.py` (908 lines), `science/bo/problem.py` (1,086) and
`connectors/bo/server/tools.py` (730) — 2,724 lines, zero files changed. The one thin slice that
*was* examined (`campaign_id_for`) found a real identity defect immediately, so the absence of
findings elsewhere was an absence of looking.

It is also the most expensive place in this repository for a plausible wrong answer. BO output is
the experimental recommendation a chemist runs at the bench: a defect costs reagents and weeks, not
a re-render.

This ADR records an audit of that subsystem against the real BoFire 0.4.1 / BoTorch 0.18.1 stack
and a live Postgres. **Most of it held.** Eight specific hypotheses were refuted by measurement and
are recorded below, because a refutation is the evidence that the next reviewer does not need to
re-run — and because this repository's own rule is that prose is evidence about its author's
beliefs, never about the code. Two defects were real, and both are on the connector surface rather
than in the mathematics.

## Decision

### 1. A categorical level the problem never declared is refused, not answered

`predict_outcome` documents its out-of-domain behaviour without qualification: *"Out-of-range points
are answered, not refused — with `in_domain: false` and a much wider sd, because the model
extrapolates rather than clamping."* That is true of a continuous **range** and false of a
categorical **level**, and the difference is not a policy choice — a range has an outside the GP can
extrapolate into, and a category does not. A ligand that was never declared has no column in the
encoder at all.

Measured, on a problem declaring `solvent ∈ {THF, toluene}` and `temperature ∈ [20, 120]`:

| point | result |
| --- | --- |
| `{temperature: 400, solvent: THF}` | answered, `in_domain: false`, sd 10.79 against 3.85–4.09 in range |
| `{temperature: 60, solvent: "DMF"}` | `KeyError: "None of [Index(['DMF'], dtype='str')] are in the [index]"` |
| `{temperature: "hot", solvent: THF}` | `TypeError: can't convert np.ndarray of type numpy.object_` |
| `{temperature: 60, solvent: 2}` | `KeyError: "None of [Index([2], dtype='int64')] are in the [index]"` |

Neither a `KeyError` nor a `TypeError` is a `ValueError` or one of the engine's
`_SURROGATE_FAILURES`, so `chemclaw.connectors.server` replaces all three with *"an internal error
occurred"* — the string its own docstring calls something "nothing can be repaired from". The model
gets no repair and retries.

**The root cause is an asymmetry in BoFire, not in the caller.** `strategy.tell` runs
`validate_experimental`; `strategy.predict` runs nothing. So the *identical* mistake in an
**observation** already comes back as a clean, forwarded `ValueError` — measured, both directions:

```
observation {ligand: "L9"}  -> ValueError: invalid values for `ligand`, allowed are: `['L1','L2','L3']`
observation {T: "hot"}      -> ValueError: not all values of input feature `T` are numerical
```

`_require_points_match` existed precisely to give the points path the parity the observation path
gets for free, and it only ever compared parameter **names**. It now checks values too, and the two
paths agree.

**It is deliberately not `point_in_domain`.** That predicate returns `False` for the 400 °C point
*and* for `"DMF"`, so validating through it would have turned a documented, useful answer into a
refusal — the over-fix is pinned shut by
`test_a_point_outside_a_continuous_bound_is_still_answered`. The narrower question this asks is
whether the surrogate has an *encoding* for the value, not whether the value is inside the space.

`bool` is excluded from the numeric branch explicitly: it is an `int` in Python, so `True` would
have reached the surrogate as a temperature of 1.0 and been answered with full confidence.

The tool docstring is corrected in the same change, because it was the thing making the promise.

### 2. A batch that could not be filled says so

`propose_candidates` returns fewer than `n` when a finite space has run low. That is correct and is
unchanged — its own docstring argues the case, and the durable loop stops on `space_exhausted`
before it can happen, so this only ever shortens the *inline* answer, which is the chemist-facing
one.

What was missing is that nothing said so. Measured on a 2×2 all-categorical space with three of its
four conditions run, asking for a batch of three:

```
asked for 3, got 1
summary: "Candidate 1: posterior sd +/-18.7 against an observed spread of 45 (42%) —
          a step beyond the runs supplied, but not a leap."
```

Every word is a reading of the one candidate that exists, and nothing marks the two that do not.
BoFire itself emits `UserWarning: Expected 3 candidates, got 1` into a log nobody reads, while the
model composing the chemist's answer had no signal that the shortfall was exhaustion rather than
selectivity. This is the shape D-2026-08-08-a-partial-answer-must-say-so settled for `science/`
(a truncated scan reporting "a genuine negative result"), reached independently on the BO surface.

`ExperimentSuggestion` now carries `requested`, and `summary` leads with the shortfall when there is
one. The clause is placed first because it changes what the rest of the summary means. `requested`
defaults to `0` = "not stated", so the several tests that build an `ExperimentSuggestion` directly
to assert a sentence — the summary is a pure function of its fields — claim no false shortfall.

## What was measured and refuted

Each of these was a specific suspicion, and each is wrong. Recorded so they are not re-audited.

1. **Objective sense.** On `y = (T-30)²` over `T ∈ [20,120]`, `minimize` proposed 27.76 / 36.96 /
   32.61 (true minimum 30) and `maximize` proposed 99.53 / 120.0 / 120.0 (true maximum 120). MOBO
   over `maximize yield` + `minimize impurity` spread along the trade-off rather than collapsing to
   one axis. No sign is flipped anywhere.
2. **Constraint sense.** All three relations honoured, on both the seeding and the model-guided
   path: `a+b <= 3` → 20/20 random seeds satisfied (max sum 2.965), `a+b >= 3` → 20/20 (min sum
   3.037), `a+b == 3` → 20/20 at exactly 3.000. SOBO proposals satisfied each. The `>=`-by-negation
   in `_constraint` is correct.
3. **Fit-quality attribution.** Two objectives with deliberately opposite predictability — one
   exactly linear in `T`, one a fixed scramble — scored R² 1.0000 and 0.0454 respectively, and kept
   their scores when the objectives were declared in the other order. `_fit_quality_from`'s
   match-on-output-key rather than on position works.
4. **Input-order determinism.** Parameter declaration order (`[T, equiv, solvent]` vs
   `[solvent, equiv, T]`) and observation order (as-run vs reversed) both left the recommendation
   identical at every printed digit. The campaign-id lane's finding about *category* order moving
   the optimizer at the 8th decimal does not generalize to these two.
5. **Design resolution.** `_resolution` derives the shortest defining word by hand rather than
   parsing BoFire's alias prose. Checked against standard tables at 3–8 factors: 2^(7-1) → VII,
   2^(7-2) → IV, 2^(7-3) → IV, 2^(5-2) → III. `get_generator` emits no sign characters at any
   combination that validates, so the `frozenset(word)` parse has nothing to mis-read.
6. **Categorical round-trip and bounds.** Across full and fractional designs, mixed and
   all-categorical: every run used a declared label, every run was distinct, and every continuous
   value sat inside its declared bounds. `n_center=2` on two categoricals gave 8 centre rows
   (per categorical combination), which is what the docstring claims. `randomize` is reproducible
   under one seed and differs across two.
7. **Persistence.** Record → resume round-trips identically under the in-memory *and* the live
   Postgres store: history accumulates, category order does not fork the id, a widened bound does
   fork it and correctly resolves to no history. No observation was lost, invented or double-counted.
8. **Observation-value validation.** Suspected as the same hole as the points path. It is not —
   BoFire's `tell` already refuses both bad-value directions with a forwarded `ValueError`, which is
   why this change touches only the points path.

## Consequences

Two new refusals exist where there were two unrepairable internal errors, and one model-facing
summary gained a clause. Nothing in the optimization mathematics changed, and no recommendation
moves: the refused calls previously produced no answer at all, and the shortfall clause is added
beside candidates that were already returned.

The `requested` field is additive on a **return** type, so no stored payload and no in-flight
campaign is affected — `Suggestion` persists candidates, never an `ExperimentSuggestion`.

`generate_screening_design` on a shape with no available generator (4 factors, `n_generators=2`)
still surfaces BoFire's own `ValueError: No generator available for the requested combination.` It
is forwarded rather than sanitized, so the model can act on it, but it names neither the factor
count nor the fix; that is a message improvement recorded in `BACKLOG.md`, not a defect.
