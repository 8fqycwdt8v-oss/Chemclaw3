"""Durable BoFire BO campaigns (plan step 1d.4).

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

from pydantic import Field
from pydantic_settings import BaseSettings


class BoSettings(BaseSettings):
    """Durable BoFire BO campaigns (plan step 1d.4).

    Grouped because these knobs shape one thing: how a Bayesian-optimization campaign runs
    durably — its per-round activity budget and heartbeat, reproducibility seed, the round and
    evaluation ceilings a spec is refused above, and the two bounds that keep a model-supplied
    decision space from costing unbounded CPU and memory to enumerate.
    """

    # A single round (BoFire propose + evaluate) can be slow, so activities get a generous
    # start-to-close budget.
    bo_activity_timeout_seconds: float = Field(default=300.0, gt=0)
    # How long a BO activity may go without a heartbeat before Temporal declares the worker dead
    # and retries (Conn-F2). Comfortably shorter than `bo_activity_timeout_seconds` so a dead
    # worker is noticed well before the full start-to-close budget — the same discipline
    # `xtb_job_heartbeat_timeout_seconds` applies to calc's durable jobs, sized down for a
    # per-round budget an order of magnitude smaller.
    bo_activity_heartbeat_timeout_seconds: float = Field(default=60.0, gt=0)
    # Seed for BoFire's random design + SOBO strategies, so a campaign is reproducible
    # (deterministic seeding + proposals) rather than flaky run-to-run.
    bo_seed: int = 42
    # Ceiling on a campaign spec's round count — a *budget* bound, not a Temporal one. It used to
    # be described as protecting the event-history limit, and did not: history is re-sent to the
    # propose activity every round, so bytes grow quadratically and a measured 178 bytes per
    # observation puts a batch-1 campaign past the 50 MB hard limit at round 441, inside this very
    # ceiling. The workflow now continues-as-new when the server suggests it, which removes the
    # history bound entirely; what is left is that every round costs an evaluation, and a spec
    # asking for thousands is a mistake worth refusing at build time.
    bo_max_rounds: int = Field(default=500, ge=1)
    # Ceiling on a campaign spec's whole evaluation budget — `n_initial + n_rounds * batch` — and
    # the reason `bo_max_rounds` alone was never the bound its name implied. A *round* is not a
    # unit of cost: at `batch=50` a spec inside the 500-round ceiling asks for 25 000 objective
    # evaluations, each one a registered objective that may call an uncached calculator. The round
    # ceiling refuses a campaign that runs too long; this refuses one that costs too much, which is
    # the quantity a chemist and a cluster budget both actually care about. 2 000 is a working
    # default: far above any campaign this system has run (the durable tests run tens), far below
    # what an unbounded batch turns a plausible round count into.
    bo_max_evaluations: int = Field(default=2000, ge=1)
    # Ceiling on how many cells of a discrete decision space may be *enumerated*. Reached only when
    # the space carries an exclusion constraint: `discrete_candidate_count` then counts feasible
    # cells one at a time, because exclusions can overlap and inclusion-exclusion would be wrong.
    # That walk is over the full categorical cross product, which is a product of model-supplied
    # category-list lengths — ten parameters of ten options is 10^10 cells, and the walk happens
    # inside `campaign_progress`, on a request. Above this ceiling the space is reported as
    # effectively unbounded (None) instead of being counted, which is the safe degradation: the
    # exhaustion guards it feeds simply do not fire, and a space this large cannot be exhausted by
    # a campaign anyway.
    bo_max_enumerated_cells: int = Field(default=1_000_000, ge=1)
    # Ceiling on the number of runs a screening design may contain. A full factorial is the product
    # of every factor's level count, so `generate_screening_design` builds a list whose length is
    # exponential in a model-supplied parameter count before anything bounds it — the same
    # unbounded-model-input shape `fingerprint_max_top_k` guards on the search side. 4 096 is far
    # past any design a human runs (a 12-factor two-level full factorial) and far below what
    # exhausts a pod.
    bo_max_design_runs: int = Field(default=4096, ge=1)
    # How many recent evaluations `science.bo.progress` reads for its "have the last N results
    # moved at all" statement, and how many consecutive noise-sized evaluations make a plateau.
    # Five is a working default rather than a statistical claim: it is short enough that a chemist
    # asking mid-campaign gets an answer about recent work, and long enough that one flat pair does
    # not read as convergence. The caller overrides it per question.
    bo_plateau_window: int = Field(default=5, ge=1)
    # Below this many evaluations `campaign_progress` refuses a plateau verdict instead of giving
    # one. A trend read off three points is the failure the whole tool exists to prevent, so the
    # floor is stated rather than left to the caller's judgement.
    bo_plateau_min_observations: int = Field(default=6, ge=2)
    # Folds for the cross-validated fit quality behind a recommendation (W5). Five is BoFire's own
    # working default and costs five refits of a model that fits in well under a second at campaign
    # sizes; the caller overrides it per question.
    bo_cv_folds: int = Field(default=5, ge=2)
    # Below this many observations a cross-validated score is reported *with* the caveat that it
    # will be over-read. Twenty is a judgement, not a threshold anyone derived: it is roughly where
    # a five-fold split stops holding out two or three points per fold. The number is here rather
    # than in the summary string because the sentence a chemist reads should not be a magic number
    # in a docstring.
    bo_fit_quality_trustworthy_observations: int = Field(default=20, ge=2)
    # How long a measured campaign's round stays open before it expires unanswered
    # (D-2026-08-29-a-decision-that-waits-is-a-workflow). A plate turnaround is the unit here, not
    # a machine timeout: fourteen days is two working weeks, which is long enough that a batch
    # genuinely in progress is not abandoned and short enough that a campaign nobody is running
    # stops asking. Clamped against `awaiting_max_days` by `open_pending_request_activity`.
    bo_measurement_deadline_days: float = Field(default=14.0, gt=0)
