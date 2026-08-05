# D-2026-08-05-a-ceiling-that-does-not-hold — A ceiling that does not hold, and four writes that could tear

**Status:** accepted · **Date:** 2026-08-05

## Context

A deep review of the whole BO layer — the engine, the campaign store, the durable workflow, and
the MCP surface the agent reaches — turned up twenty-eight findings. This ADR records the ones
that changed code, the one that changed nothing because measuring it showed the existing
behaviour was right, and the one deliberately left open.

Everything below was measured. Prose is evidence about what its author believed.

## Decision

### 1. A finite decision space that has been fully run is refused, in a sentence

`suggest_next_experiment` went straight to BoFire's `ask()`. When every cell of a discrete space
has already been run, `_optimize_acqf_discrete` drops the run rows, hands an empty frame to
`domain.inputs.transform`, and raises `KeyError: '<parameter name>'`. That is neither a
`ValueError` nor one of `_SURROGATE_FAILURES`, so `connectors/server.py` correctly refuses to
forward it and the model receives "an internal error occurred" — nothing it can repair, so it
retries, forever.

**This is the unproven bug.** `_require_observed_params_match`'s docstring records a live
`KeyError: 'base'` from this exact BoFire frame that its author could not reproduce and wrote up
as unproven. It is exhaustion, not a parameter mismatch — which is why driving mismatched
parameters never reproduced it. W4 made it reachable sooner: an exclusion removes cells, so a
2×2 minus one pairing exhausts after three runs rather than four.

`_require_fresh_points_exist` refuses at **zero** fresh points, not at `space_exhausted`'s
"cannot fill a batch". Returning two of three asked-for candidates is honest; refusing the ask
would not be.

**Not fixed with a wider `except`.** Adding `KeyError` to `_SURROGATE_FAILURES` was tried and
reverted: `test_propose_candidates_does_not_swallow_unrelated_errors` exists to stop that, and it
is right — wrapping a `KeyError` would misdiagnose a code defect as bad chemistry data *and* make
it non-retryable on the durable path, telling a chemist to "vary the inputs" about a bug in this
repository. A cause we understand gets a sentence; one we do not gets a stack trace.

### 2. The campaign write is one transaction, refuses non-finite floats, and no longer swallows our bugs

Three faults in one write path:

- `upsert_campaign` and `add_suggestion` were two statements from two `_connect()` calls, so a
  failure between them left a campaign row with no suggestion, or lost a suggestion whose campaign
  never landed. They are now one `record()` inside one `conn.transaction()`.
- Both JSONB columns went through `Jsonb`'s default `json.dumps`, which happily writes `NaN` and
  `Infinity` — tokens no conforming JSON reader parses back. A non-finite objective is now refused
  at the store, by the write that produced it, rather than at whatever later read fails on it.
- `record_suggestion` caught bare `Exception`. A `TypeError` or a `ValidationError` in the payload
  read as "the database blinked" and the suggestion vanished with a WARNING. Narrowed to
  `(ConnectionError, OSError, TimeoutError, psycopg.Error)`: a deployment where 100% of BO writes
  fail must not be indistinguishable from one where none do.

### 3. The round ceiling is re-described as what it bounds, and the workflow continues-as-new

`bo_max_rounds` defaults to 500 and both its config comment and `require_rounds_within_ceiling`'s
docstring said it existed to keep the campaign inside Temporal's event-history limit.

Measured: 178 bytes per serialized `Observation`; the history is re-sent to `propose_next` every
round, so bytes grow quadratically; a batch-1 campaign crosses the 50 MB hard limit at round
**441** — inside the ceiling that was supposed to prevent exactly this. The server would terminate
it there and every already-paid evaluation would be lost.

`_carry_on_if_history_is_filling_up` continues the campaign in a fresh run when Temporal's own
`is_continue_as_new_suggested()` flips. That signal rather than a round count, because batch size,
parameter count and objective count all change bytes-per-round, so any number hard-coded here
would be right for one problem shape and wrong for the rest. It is called after a round completes,
never between propose and evaluate, so nothing already paid for is abandoned. The carried state is
`CampaignCarryOver` — the observations and the rounds still owed; the spec is immutable and rides
along as the unread payload.

The ceiling stays at 500 and is re-documented as an **evaluation budget** bound, which is what it
now is.

### 4. Two categories with the same descriptor row are refused

Featurizing replaces a label with a position in descriptor space, and BoFire's
`CategoricalDescriptorInput` gives the model only the position. Two categories at one position are
one point to the surrogate.

Measured, before the guard: a two-descriptor parameter whose `A` and `B` rows matched, with A
observed at 10 and B at 90 — `predict_at` returned **70.85 for each**. No warning, no error; a
confident recommendation for a reagent the model has never distinguished from another.

Two plausible routes in: two labels pointing at the same SMILES (`"Pd(OAc)2"` and
`"palladium acetate"`) featurize identically by construction, or a caller supplies `descriptors`
directly and repeats a row.

`require_descriptors_distinguish_categories` sits **outside** the model, for the reason
`require_names_do_not_clash` does: nothing forbade this before, so a stored or in-flight campaign
may carry it, and `OptimizationProblem`'s validators re-run at workflow replay and on every
`resume_campaign` read. A model-level rule would strand those rather than refuse a new one. It is
called after featurization, since featurization is what creates the collapse.

### 5. The JSON-string tolerance got a floor under it

The model sometimes emits an array JSON-*encoded* as one string — a live e2e finding worth keeping
the tolerance for. But three call sites did `json.loads` and iterated the result unchecked, and
`json.loads` decodes *any* JSON: `"null"` became `None`, `"42"` an int (`TypeError`, reported as an
internal error), `"{}"` a dict whose **keys** were then validated as observations, failing with a
message about strings that were never runs. One `_as_list(value, noun)` helper, three call sites.

### 6. `evaluate_candidates` heartbeats inside a candidate as well as between them

It beat between candidates only. A registered objective is not guaranteed fast —
`solubility_objective` calls an uncached calculator — so one candidate slower than
`bo_activity_heartbeat_timeout_seconds` went silent mid-evaluation, Temporal declared the worker
dead, and the activity retried from the top, re-paying every candidate already evaluated in the
batch. A one-candidate batch had no protection at all.

Both beats are kept, and both are needed: the one *between* candidates carries the honest progress
report and keeps a long batch of fast candidates alive, where no single evaluation runs long enough
for a timer to fire.

### 7. The `state_changing` classification is derived from the code, not restated beside it

`tests/test_bo_tools.py` parses the tool module and asserts that every `@server.tool()` calling
`featurize_problem` or `record_suggestion` appears under `state_changing` in the manifest. The
partition **fails open**, so a tool wrongly listed read-only ships an ungated spend that looks
exactly like a gated one — and both BO tools that featurize were listed read-only until a review
traced the call chain. A test restating today's names would stay green the day someone adds
featurization to `campaign_progress`, which is the only change worth catching. Verified to fail
by removing `predict_outcome` from the manifest.

## Measured and rejected

**`ModelFittingError` / `NotPSDError` are correctly non-retryable.** The proposal was to split them
out of `_BAD_DATA_TYPES` as transient. They are not: `_resolve_seed` gives every fit a fixed seed
(config `bo_seed`, or the campaign's own), and seed-determinism was measured in M-1(e). A retry is
therefore a re-run of the identical computation on identical data, and `SurrogateFitError`'s
existing message — duplicate or degenerate points collapsing the kernel — describes the real cause.
Making them retryable would burn `activity_max_attempts` re-fitting the same failure.

## Declarations corrected

Five statements that outlived what they described — the class this repo keeps producing, and the
reason D-2026-08-05-a-declaration-outliving-what-it-describes exists:

- `connectors/bo/server/tools.py` called the whole surface "read-only *capability*". Two of five
  tools featurize and one also writes two tables.
- `experiment-design/SKILL.md` said `predict_outcome` "does not record anything". True of the
  campaign tables, false of the calculation cache.
- `data/evals/probes/optimization.yaml`'s `op-13` expected `suggest_next_experiment` — the tool that
  existed when the probe was written, and the *reflex* its own `direction` field calls out.
  `campaign_progress` and `predict_outcome` now count too (`evals/live.py` matches with `any`).
- `DEFERRED.md`'s nonlinear-constraints row called pymoo "a new dependency". Measured:
  `pymoo==0.6.2` already ships transitively with `bofire[optimization]` and
  `GeneticAlgorithmOptimizer` imports without installing anything. The real objection — a worse
  acquisition optimizer for no stated use case — survives; the cost half is deleted.
- `bo-capability-map.md` §1 described the wiring as it stood before W1–W5. It is now marked as the
  pre-wave snapshot it is (which is what makes it useful), with the rows the waves moved corrected.

## Left open

**The durable campaign does not write the campaign store.** Investigated as asked, and it is a gap
rather than a design choice: both paths share one campaign-id space, so `resume_campaign` on a
campaign that ran durably reports no such campaign about work that was actually done.

Not closed here, because closing it needs a decision this ADR should not make quietly.
`record_suggestion` writes `opened_by`, and `BoCampaignWorkflow` deliberately does not know the
actor — core's `ConnectorJobWorkflow` owns attribution, and the connector contract is
payload-in/envelope-out (D-093). Recording from inside the workflow means either threading identity
through a seam built to keep it out, or writing a fabricated actor into an audited column. Neither
belongs in a review fix. It is also the one item here that could not have been verified in this
environment: both Temporal's test server and Postgres are unreachable offline.

Recorded in `docs/planning/BACKLOG.md`.

## Consequences

- A completed screen gets a sentence telling the chemist it is complete, instead of an infinite
  retry loop against an internal error.
- A long campaign survives its own length.
- A featurization that cannot distinguish two reagents is refused instead of answering one number
  for both.
- A BO write either lands whole or does not land, and our own defects surface as defects.
