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
    # Ceiling on a campaign spec's round count. The observation history is carried as workflow
    # state and re-sent to the propose activity every round, so history bytes grow quadratically
    # with rounds and an unbounded spec would hit Temporal's hard event-history limit mid-run,
    # losing every already-paid evaluation. Generous versus the default of 10 rounds; a spec
    # beyond it is rejected at build time, not terminated by the server mid-campaign.
    bo_max_rounds: int = Field(default=500, ge=1)
