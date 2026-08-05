# D-2026-08-04-a-trade-off-has-no-single-best-point — A trade-off has no single best point

**Status:** accepted · **Date:** 2026-08-04 · **Implements:** W3 of
D-2026-08-04-what-bofire-does-when-you-actually-run-it · **Extends:**
D-2026-07-31-a-campaign-is-an-entity-not-a-turn (the campaign-id hash)

## Context

`OptimizationProblem` carried one `objective`. Meanwhile every ELN run already records
`yield_percent`, `purity_percent` **and** `impurities[].area_percent`: the corpus stores a trade-off
the optimizer could only be told one side of. Story 3.3 was `PARTIAL` with multi-objective marked
*unrepresentable*, the tool spent a bold paragraph instructing the model to refuse, and live probe
`op-16` — *"maximise yield and minimise the des-bromo impurity at the same time, give me the Pareto
front"* — was graded **fabricated** for promising to call the tool "with both objectives" anyway.

The refusal was correct and was not enough: an instruction not to want something is a poor
substitute for the thing.

## Decision

**`objectives: list[Objective]`, one field.** Not a lead objective plus a sidecar list: the sidecar
shape guarantees a lone objective sometimes lands in the wrong one, and it bakes a "primary" fiction
into a front where every axis is symmetric.

**The old spelling is accepted permanently, not for a migration window.** A `mode="before"`
validator turns `{"objective": …}` into a one-element list. That spelling is in every
`bo_campaigns.problem` JSONB row and in every in-flight `CampaignSpec` in Temporal history, where
validators re-run at **replay** — rejecting it would fail a running campaign, which is the exact
hazard `require_rounds_within_ceiling`'s comment documents one field over. Both spellings at once is
refused: it means the writer believed two different things.

**An `objective` property, not a field.** It returns `objectives[0]` and is deliberately
unserialized, so the wire shape is `objectives` and nothing else. Every one of the 13 readers across
six modules wanted `.name` or `.direction`, and none of those readings changes when a second
objective appears. The lead objective is privileged in exactly two places, both display or identity,
never optimization: the `bo_campaigns.objective`/`direction` columns and the legacy half of the id
hash. **Source code says `objectives=`**; the singular spelling is for data on disk, and mypy
enforces that split by not knowing about the validator.

**`best_of` raises on a trade-off; `pareto_front` is the answer.** Returning the lead objective's
winner would report "the best conditions" for a campaign whose premise is that no such point
exists — the same overclaim `op-16` was graded for, arrived at from the opposite direction. The
front is hand-written dominance in pure Python, deliberately not `bofire.utils.multiobjective`:
`problem.py` is the campaign job's `params_model` and is imported into the agent process, which
`tests/test_connector_isolation.py` exists to keep `torch` out of.

**The front is over the runs the caller supplied, not over predictions.** A trade-off is a statement
about measurements. `ExperimentSuggestion.front` holds the non-dominated subset of the observations
handed in, and the summary says so.

**Scalars stay beside the vectors.** `Observation.value` and `Candidate.predicted_value`/`_sd` keep
the lead objective and remain required; `values`/`predicted_values`/`predicted_sds` are name-keyed
maps that are empty on a single-objective problem. Symmetry was tempting and would have cost four
persistence surfaces: an `Observation` does not know its problem, so a legacy `{"value": 0.83}` has
no objective name to key on. One boundary check enforces that they agree.

**Multi-objective is inline only.** `bo.objectives`'s registry maps a name to
`Callable[..., Awaitable[float]]` — one number per evaluation — so a multi-output registry would be
an abstraction with zero real callers (Rule of Three). `require_campaign_startable` folds the round
ceiling and a multi-objective refusal into the one function `connector.yaml` may name, and
`require_campaign_within_ceiling` is deleted rather than left as a second entry point.

**`campaign_progress` refuses an unnamed objective on a trade-off.** A plateau is per axis — yield
can stop moving while the impurity is still falling — so reading the lead one silently would answer
a different question from the one put. Same posture as `best_of`, for the same reason.

## The campaign-id hash

The identity dump used a **denylist** (`exclude={"descriptors"}`), so adding any field to a parameter
would fork every campaign id in the database, invisibly: a new id, an empty history, and
`read_campaign_thread` telling each chemist their campaign is new. Two changes together — an
allowlist (`{kind, name, lower, upper, categories, structures}`), and the new keys entering the
identity dict **only when non-empty** — keep a single-objective unconstrained problem hashing to the
byte-identical payload.

M-2 captured three baseline ids from `main` before the migration, and they are now hard-coded in
`tests/test_bo_campaign_record.py`: `campaign-6958b7edaa261c83`, `campaign-55e5f929fe83a9a5`,
`campaign-109f34eac28892ab`. Nothing else in the repository pinned an absolute id, so a global
rehash would have passed every existing test. **No SQL migration** — the columns keep the lead
objective and the `direction` CHECK still holds.

## Two artefacts that became wrong, and were changed rather than left

**The tool description.** "One objective, no constraints … they are unrepresentable" was true of
both halves and is now true of one. A refusal that outlives its refusal teaches the model to decline
a capability that exists — so the objective half is replaced with what *is* supported, the
constraint half survives verbatim until W4, and the test that pinned the sentence now asserts both
directions rather than being inverted wholesale.

**The eval probe.** `data/evals/probes/optimization.yaml`'s `op-16` graded the model on *refusing*
multi-objective. Left alone it would have marked the correct new behaviour as a failure. Its
`forbids_claims` now name the overclaims that are actually available here: that these conditions are
the single best point, that the front is a prediction rather than the non-dominated subset of the
runs supplied, and that the optimizer proved no better trade-off exists. `op-17` (constraints) keeps
its refusal, with one stale clause corrected.

## Consequences

- Story 3.3's objective half is served; the constraint half remains `UNREPRESENTABLE` until W4, and
  the tool now says exactly that rather than one sentence covering both.
- The `experiment-design` skill's framing rule reverses — "give them all" where it said "pick the one
  they lead with". It is the only instruction in that file to have flipped, and it is labelled as
  such so a reader does not take it for drift.
- `MoboStrategy`'s reference point is derived per objective from the data
  (`AbsoluteMovingReferenceValue`, measured in M-1) rather than fixed by us. That is a real modelling
  choice made by the library, and it is recorded here because a hidden reference point silently
  defines what "better" means.
- The durable campaign is unchanged and still runs `best_of`, because it can only ever be
  single-objective. Nothing about Temporal history or the activity contract moved.
