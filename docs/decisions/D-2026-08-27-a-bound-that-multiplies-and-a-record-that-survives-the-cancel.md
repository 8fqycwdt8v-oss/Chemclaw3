# D-2026-08-27-a-bound-that-multiplies-and-a-record-that-survives-the-cancel — a ceiling on rounds is not a ceiling on cost

## Status

Accepted, 2026-08-27.

## Context

A deep audit of the BoFire layer — engine, connector, persistence, tests, and the frontend that
renders none of it — re-checked every decision this tree has recorded about Bayesian optimization
against the code rather than against the ADR that claims it. The reassuring half first, because it
decides how much of the rest to believe: **every historical numerical fix checked out.** The
inverted-direction guard is live in `require_campaign_startable`, the over-precise
cross-validation score reports to two significant figures with its repeatability caveat, the
fractional-factorial cross-product fix is in `_full_design` with its measured before/after numbers
in the docstring, and the campaign write is one transaction refusing non-finite floats. No case was
found where an ADR described a fix the code had not made.

What the audit did find is a different class of defect, and it clusters. Five findings from the
2026-08-16 round-1 audit were **confirmed at the time and never fixed**; the four that share a
shape share it exactly. Each is a *bound that does not bound what its name says*, or a *count whose
two sides are counted differently*.

## Decision

### 1. The evaluation budget is bounded, not just the round count

`require_rounds_within_ceiling` bounds `n_rounds` and its docstring says "every round costs a real
evaluation". A round costs `batch` of them, and `batch` had no ceiling. `n_rounds=400, batch=50`
sits inside the 500-round ceiling and asks for 20 000 objective evaluations, each one a registered
objective that may call an uncached calculator. The ceiling written to refuse "a spec that would
spend thousands of evaluations" permitted exactly that, because it never multiplied.

`require_evaluations_within_budget` bounds `n_initial + n_rounds * batch` against
`bo_max_evaluations` (default 2 000), beside its sibling in `require_campaign_startable` and for
the same reason not on the model: `CampaignSpec`'s validators re-run at workflow replay, where a
lowered ceiling must not fail an in-flight campaign's own input.

### 2. Both sides of the exhaustion comparison are counted the same way

`discrete_candidate_count` counts *feasible* cells — W4 made it exclusion-aware, so 2×2×2 minus one
forbidden pairing is six, not eight. The history it was compared against was counted with no such
filter. An observation of an *excluded* point therefore consumed one of six cells it was never part
of, and both consumers failed in the same direction: `space_exhausted` stops a durable campaign with
fresh points left, and `_require_fresh_points_exist` refuses an inline ask with "the screen is
complete" while cells remain.

The trigger is ordinary rather than adversarial, which is what makes it worth fixing rather than
documenting: a chemist learns a pairing decomposes *after* running it, adds the exclusion, and keeps
the measurement — the correct thing to do with a real run, and exactly the input that skews the
count. Every campaign carrying prior lab work under a later-added exclusion hits it.

`point_is_feasible` is now the one definition of "this run occupies a cell of the design space", and
`distinct_feasible_candidate_count` is what both consumers compare against. `distinct_candidate_count`
stays, unfiltered, for the progress report: a chemist who ran a condition later excluded still ran
it, and reporting otherwise would deny an experiment that happened.

### 3. Two unbounded model-supplied inputs get ceilings

Both are the `fingerprint_max_top_k` shape — a number the model writes, reaching something that
allocates — which three separate round-1 findings named as the pattern this surface should mirror
and which none of the four proposed settings had yet.

- **`discrete_candidate_count`'s enumeration** walks the full categorical cross product whenever an
  exclusion is present. "This space is small by construction" was an assumption about the caller,
  not a property of the input: ten parameters of ten options is 10^10 cells, and `campaign_progress`
  reaches it synchronously on a request. Above `bo_max_enumerated_cells` (default 1 000 000) it
  returns `None` — the same "effectively unbounded" a continuous space already returns, so the
  exhaustion guards simply do not fire, which is correct because a space that size cannot be
  exhausted by any campaign this system can run.
- **`factorial_design`'s run count** is the product of every factor's level count. Twenty two-level
  factors is 1 048 576 rows materialized as a list before anything downstream sees it.
  `_require_design_fits_the_ceiling` refuses above `bo_max_design_runs` (default 4 096) and decides
  from the *count*, never from the built design — a bound that trips once the list exists has
  already paid the memory it exists to protect. It lives in the engine and not in the MCP tool,
  following the fleet's own rule that a bound in the transport is one the in-process callers do not
  get.

### 4. `campaign_progress` runs off the event loop

It was the one compute-bearing tool on this surface doing its work on the loop thread, while
`suggest_next_experiment`, `predict_outcome` and `generate_screening_design` beside it already
offloaded. With §3's walk above it, a wide decision space stalled every other request the server was
serving. This is the other half of that fix, not a substitute for it.

### 5. A campaign's history survives everything except a normal ending — now it survives those too

`BoCampaignWorkflow` wrote the campaign record **once**, after its round loop. Every round a running
campaign had already paid for lived only in Temporal's event history until then, so a campaign
cancelled, terminated, or failed non-retryably mid-run answered `resume_campaign` with "no such
campaign" about hours of real evaluation. That is the same gap the terminal write closed for a
campaign that *finishes* (D-2026-08-05), left open for every other ending.

The write now happens per round, keyed `"{workflow_id}:r{n}"` — because `record_suggestion`'s
idempotency is `(campaign_id, job_id)`, and a per-round write under the bare workflow id would
dedupe against round 1 and silently discard every round after it. `CampaignCarryOver` gains
`rounds_done` so a continue-as-new does not restart the index and collide with the previous run's
rows.

Two things fall out of this that were separately on the audit's list, and neither needs its own
mechanism:

- **"Pause a campaign" needs no signal handler.** Cancel-then-resume is the same thing once the
  history survives the cancel, and Temporal's own cancellation is already exposed through core's
  `cancel_job`. A `@workflow.signal` pause would be a second way to do what cancel now does
  correctly.
- **The predictions survive.** The terminal write records `Candidate(params=result.best.params)` —
  the best *point*, which has no surrogate belief attached to it. The per-round rows record the
  candidates actually proposed, carrying their `predicted_value`/`predicted_sd`, so a campaign's
  predictions are now recoverable where before they reached the database on no path at all.

### 6. The fork signal comes from the write, not from a read before it

`suggest_next_experiment` read `campaign_is_known` and then wrote. Two turns opening the same
decision space concurrently both read no campaign and both reported having opened one, while the
upsert underneath serialized them — so exactly one was right and nothing could tell which.

`_UPSERT_CAMPAIGN` now returns `(xmax = 0)`, Postgres's own answer to "did this insert or update",
and `record_suggestion` returns a `RecordedSuggestion` carrying it. The read it replaced is deleted:
`campaign_is_known` had no other caller, and a guard kept alive by nothing is the shape this tree
deletes on sight. One round-trip fewer, and an answer that cannot disagree with what was written.

### 7. `CampaignThread` no longer carries `opened_by`

The column stays and keeps doing its audit job. What it must not do is travel back out to a model
and thence to a chemist as provenance, because on the inline path it holds whatever
`X-Chemclaw-Actor` claimed, recorded as `unverified:<id>` precisely because nothing authenticated
it. Rendering an unauthenticated self-assertion as "opened by" is the shape
`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` deleted elsewhere: an identity
claim the system cannot stand behind is worse than none, because a reader cannot tell which they are
looking at. Who opened a campaign is answerable from the audit trail, by someone who can see whether
the actor was verified.

### 8. One statement of the rules a single-best-point loop needs

`science.bo.campaign.optimize` and `require_campaign_startable` each wrote out the names-clash,
descriptor-collision and single-objective checks, with two different wordings of the same
multi-objective refusal. `require_problem_yields_one_best_point` is now the one statement both call.

## What was deliberately *not* done

**Multi-objective was not wired into the durable campaign.** The audit filed this as its largest
capability gap, and re-reading the code changed the conclusion: it is a deliberate, argued refusal,
not an oversight. `bo.objectives` maps a name to `Callable[..., Awaitable[float]]` — one number per
evaluation — and there are two registered objectives, both scalar. A multi-output registry would be
an abstraction with zero real callers, built to make a refusal message unnecessary, which is what
this repository's Rule of Three exists to prevent. The refusal already names the inline tool that
does do multi-objective. This stays a refusal until something needs to register a vector-valued
objective; then it is a decision with a caller behind it.

**The dead-ish `science/bo/campaign.py` was not deleted.** The audit flagged it as a module with no
`src/` caller. It has three test callers that carry real weight — `test_reizman.py`'s beat-the-median
quality bar and `test_bo.py`'s convergence checks — and deleting it would inline the same ask/tell
loop into three test files, which is worse duplication than the one it removes. The *actual* defect
it was flagged for, the triplicated preconditions, is §8 above.

## Consequences

Four new settings (`bo_max_evaluations`, `bo_max_enumerated_cells`, `bo_max_design_runs`, plus the
existing ceiling now correctly described). One protocol change: `CampaignStore.record` returns
`(suggestion_id, created)` and `record_suggestion` returns `RecordedSuggestion` rather than a bare
id — both backends implement it, and the in-memory one is a real deployment backend rather than a
test double, so the contract had to move on both sides together.

The per-round write costs one INSERT per round. A round is a BoFire fit plus a batch of objective
evaluations — seconds at the very least, minutes routinely — so the write is not measurable against
it, and it buys back every already-paid evaluation of any campaign that does not end cleanly.

## The pattern worth carrying

Four of the seven fixes above are the same mistake: **a quantity that is checked and a quantity that
is spent, differing by a factor nobody multiplied.** Rounds against evaluations. Feasible cells
against distinct runs. A ceiling on a count against the product that count came from. This tree's
own stated lesson — "prose is evidence about what its author believed, never about what the code
does" — has a corollary this layer keeps needing: *a bound's name is prose too.* `bo_max_rounds`
reads as a cost ceiling and bounds a loop counter; the two differed by `batch`, silently, for as
long as batching existed.
