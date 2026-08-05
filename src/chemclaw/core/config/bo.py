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

    Grouped because these four knobs shape one thing: how a Bayesian-optimization campaign runs
    durably — its per-round activity budget and heartbeat, reproducibility seed, and the round
    ceiling that protects Temporal's event-history limit.
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
