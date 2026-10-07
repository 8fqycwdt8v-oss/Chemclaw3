"""Settings for durable BoFire BO campaigns.

One domain section of the composed `Settings`; the package `__init__.py` flattens the sections and
owns the env prefix, `.env` loading and cross-section validators.
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

    # Start-to-close for one round (BoFire propose + evaluate), which can be slow.
    bo_activity_timeout_seconds: float = Field(default=300.0, gt=0)
    # Heartbeat timeout for a BO activity; well under `bo_activity_timeout_seconds` so a dead worker
    # is noticed before the whole round budget lapses.
    bo_activity_heartbeat_timeout_seconds: float = Field(default=60.0, gt=0)
    # Floor on one dispatch's queue wait. `BoCampaignWorkflow._queue_wait` splits the remaining
    # ceiling across the dispatches still to come; without a floor a rolling or scaled-to-zero `bo`
    # worker would expire `schedule_to_start` on a healthy campaign. Fifteen minutes covers a
    # restart.
    bo_queue_wait_floor_seconds: float = Field(default=900.0, gt=0)
    # Seed for BoFire's random design and SOBO strategies, so a campaign is reproducible.
    bo_seed: int = 42
    # Ceiling on a spec's round count: a budget bound, refused at build time. History growth is
    # handled by continue-as-new, not by this.
    bo_max_rounds: int = Field(default=500, ge=1)
    # Ceiling on a spec's total evaluations (`n_initial + n_rounds * batch`). A round is not a unit
    # of cost — a large batch multiplies it — so this bounds what a campaign actually spends.
    bo_max_evaluations: int = Field(default=2000, ge=1)
    # Ceiling on enumerating a discrete space's cells, reached only with an exclusion constraint
    # (feasible cells are counted one by one over a model-supplied cross product). Above it the
    # space is reported as unbounded (None), so the exhaustion guards it feeds simply do not fire.
    bo_max_enumerated_cells: int = Field(default=1_000_000, ge=1)
    # Ceiling on runs in a screening design; a full factorial is exponential in the model-supplied
    # factor count. 4096 is a 12-factor two-level full factorial.
    bo_max_design_runs: int = Field(default=4096, ge=1)
    # Ceiling on candidates per ask (`suggest_next_experiment`'s `count`, a campaign round's
    # `batch`); cost is linear in it. 96 is a plate. This bounds the ask, not latency;
    # `bo_max_evaluations` bounds a whole campaign.
    bo_max_candidates_per_ask: int = Field(default=96, ge=1)
    # Recent evaluations `science.bo.progress` reads for "have the last N moved", and consecutive
    # noise-sized evaluations that make a plateau. A working default; callers override per question.
    bo_plateau_window: int = Field(default=5, ge=1)
    # Below this many evaluations `campaign_progress` refuses a plateau verdict rather than read a
    # trend off a handful of points.
    bo_plateau_min_observations: int = Field(default=6, ge=2)
    # Folds for the cross-validated fit quality behind a recommendation; BoFire's own default.
    bo_cv_folds: int = Field(default=5, ge=2)
    # Below this many observations a cross-validated score carries a caveat that it will be
    # over-read (roughly where a five-fold split stops holding out two or three points per fold).
    bo_fit_quality_trustworthy_observations: int = Field(default=20, ge=2)
    # Relative spread below which a response is flat and no R² is reported (`max - min <= abs(mean)
    # * this`). Relative, not `== 0.0`, so sub-noise drift cannot score a spurious fit; 1e-9 is far
    # below any assay's resolution and far above float64 epsilon.
    bo_flat_response_relative_spread: float = Field(default=1e-9, gt=0.0, le=1e-3)
    # Days a measured campaign's round stays open before it expires unanswered (two working weeks).
    # Clamped against `awaiting_max_days` by `open_pending_request_activity`.
    bo_measurement_deadline_days: float = Field(default=14.0, gt=0)
