"""Whether an optimization is still finding anything, judged against the assay's own noise.

`assay_noise` is required: a plateau verdict needs the chemist's stated reproducibility. Gains are
measured from the last real gain, so a sub-noise climb counts once it accumulates. No BoFire import
(agent process).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from chemclaw.core.config import settings
from chemclaw.science.bo.problem import (
    Objective,
    Observation,
    OptimizationProblem,
    discrete_candidate_count,
    discrete_space_size,
    distinct_candidate_count,
    distinct_feasible_candidate_count,
    observed_value,
)


def _named(problem: OptimizationProblem, objective: str) -> Objective:
    """The named objective, or a message listing the ones this problem actually declares."""
    for candidate in problem.objectives:
        if candidate.name == objective:
            return candidate
    declared = [item.name for item in problem.objectives]
    raise ValueError(f"unknown objective {objective!r}; this problem declares {declared}")


def _improved_by(direction: str, new: float, best: float) -> float:
    """How much better `new` is than `best`, in the problem's own direction (negative = worse)."""
    return new - best if direction == "maximize" else best - new


class CampaignProgress(BaseModel):
    """Where an optimization has got to, and whether its recent runs mean anything.

    Every field is a statement about the observations supplied — this model never asks a surrogate
    what it thinks, so nothing here is a prediction. That split is deliberate: the questions "has
    the record stopped moving" and "does the model expect the next point to beat noise" have
    different evidence behind them, and answering the first with the second is how a campaign gets
    talked into another fortnight.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: str = Field(min_length=1)
    direction: Literal["minimize", "maximize"]
    # The chemist's own reproducibility figure. Required by `campaign_progress`; carried here so
    # every sentence below can be read against the number it was judged with.
    assay_noise: float = Field(gt=0)
    window: int = Field(ge=1)

    n_observations: int = Field(ge=0)
    # Distinct parameter combinations run (replicates count once), including any an exclusion later
    # forbade.
    n_distinct: int = Field(ge=0)
    # How many of those occupy a cell of the *feasible* grid — what a coverage claim may divide by.
    # A run can fall outside it through an exclusion or a value outside the current domain (e.g. a
    # category renamed on resume). Both counts are reported; `out_of_space` names the difference.
    n_distinct_in_space: int = Field(ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def out_of_space(self) -> int:
        """Distinct conditions run that occupy no cell of the feasible grid.

        Non-zero means the history holds runs the current problem cannot express: a pairing excluded
        after being run, or a value left by an edited space.
        """
        return self.n_distinct - self.n_distinct_in_space

    # The feasible grid size, or None: either genuinely infinite (a continuous parameter) or too
    # large to enumerate under an exclusion (`bo_max_enumerated_cells`). `space_is_infinite`
    # separates the two.
    design_space: int | None = None
    # True only for the first of those two: at least one continuous parameter.
    space_is_infinite: bool = False

    best_value: float | None = None
    # The running best after each evaluation, in the order supplied.
    best_so_far: list[float] = Field(default_factory=list)
    # Evaluations since a result beat the value at the **last real gain** by more than
    # `assay_noise`: the headline "last real gain was N runs ago".
    evaluations_since_improvement: int = Field(default=0, ge=0)

    # Spread of raw values over the last `window` evaluations, to compare against the stated noise.
    # None with fewer than two observations.
    window_span: float | None = None
    window_indistinguishable: bool = False

    enough_observations: bool = False
    plateaued: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summary(self) -> str:
        """The reading in words, including the limit a plateau verdict may never exceed.

        A `computed_field` so it is serialized and reaches the model composing the answer.
        """
        if not self.enough_observations:
            return (
                f"{self.n_observations} evaluation(s) is too few to read a trend from — this needs "
                f"at least {settings.bo_plateau_min_observations}. No plateau verdict is given, "
                "which is different from saying the campaign is still improving."
            )
        parts = [
            f"Best {self.objective} so far: {self.best_value:.6g} "
            f"over {self.n_observations} evaluation(s)"
            f"{self._space_clause()}."
        ]
        if self.evaluations_since_improvement == 0:
            parts.append(
                f"The most recent evaluation improved on everything before it by more than the "
                f"stated assay noise (+/-{self.assay_noise:.3g}), so the search is still moving."
            )
        else:
            parts.append(
                f"The last gain larger than the stated assay noise (+/-{self.assay_noise:.3g}) was "
                f"{self.evaluations_since_improvement} evaluation(s) ago."
            )
        if self.window_span is not None:
            distinguishable = "are NOT distinguishable from each other"
            if not self.window_indistinguishable:
                distinguishable = "do differ by more than that noise"
            parts.append(
                f"The most recent {self.window} results span {self.window_span:.3g}, so they "
                f"{distinguishable}."
            )
        parts.append(
            f"Plateaued: no further gain beyond the noise for at least {self.window} evaluation(s)."
            if self.plateaued
            else "Not plateaued on this window."
        )
        parts.append(
            "This is a reading of the runs supplied and nothing more: it cannot show that a global "
            "optimum has been reached, only that recent points in the region already explored have "
            "not beaten the noise. An untried corner of the space is not evidence either way."
        )
        return " ".join(parts)

    def _space_clause(self) -> str:
        """The design-space efficiency claim, when the space is finite enough to have one."""
        if self.design_space is None:
            return ""
        # Both sides feasible: the numerator is runs occupying a feasible cell; `n_distinct` is
        # still reported beside it.
        stated = (
            f" ({self.n_distinct_in_space} distinct condition(s) out of the {self.design_space} "
            "the feasible grid holds"
        )
        if self.out_of_space:
            # Never silently drop them: the difference is the finding, and a reader who sees only
            # the smaller number concludes they have screened less than they have.
            stated += (
                f", plus {self.out_of_space} further run(s) the current decision space no longer "
                "contains — an excluded pairing, or a value left behind by an edited space"
            )
        return stated + ")"


def campaign_progress(
    problem: OptimizationProblem,
    observations: list[Observation],
    assay_noise: float,
    window: int | None = None,
    objective: str | None = None,
) -> CampaignProgress:
    """Read a campaign's observations for a plateau, against the noise the chemist stated.

    `observations` must be in the order performed; `objective` is required on a multi-objective
    problem.
    """
    if assay_noise <= 0:
        raise ValueError(
            f"assay_noise must be positive; got {assay_noise}. It is the assay's reproducibility "
            "in the objective's own units — without it, no gain can be called real."
        )
    span_window = settings.bo_plateau_window if window is None else window
    if span_window < 1:
        raise ValueError(f"window must be at least 1; got {span_window}")

    if objective is None and len(problem.objectives) > 1:
        named = ", ".join(item.name for item in problem.objectives)
        raise ValueError(
            f"this problem has {len(problem.objectives)} objectives ({named}); name which one to "
            "read a plateau for. A trade-off plateaus per axis, and answering for the first one "
            "without being asked would report a different question than the one put."
        )
    chosen = problem.objective if objective is None else _named(problem, objective)
    direction = chosen.direction
    values = [observed_value(problem, observation, chosen.name) for observation in observations]
    best_so_far: list[float] = []
    since = 0
    best: float | None = None
    # The value at the last *real* gain; the counter measures from here rather than the running
    # best, so a sub-noise creep registers once it accumulates past the noise.
    anchor: float | None = None
    for value in values:
        # The running best is what the campaign has actually reached, and it moves on any gain —
        # reporting a stale best would misstate where the campaign is.
        if best is None or _improved_by(direction, value, best) > 0:
            best = value
        if anchor is None or _improved_by(direction, value, anchor) > assay_noise:
            anchor, since = value, 0
        else:
            since += 1
        best_so_far.append(best)

    tail = values[-span_window:]
    span = max(tail) - min(tail) if len(tail) >= 2 else None
    enough = len(values) >= settings.bo_plateau_min_observations
    return CampaignProgress(
        objective=chosen.name,
        direction=direction,
        assay_noise=assay_noise,
        window=span_window,
        n_observations=len(values),
        n_distinct=distinct_candidate_count(observations),
        n_distinct_in_space=distinct_feasible_candidate_count(problem, observations),
        design_space=discrete_candidate_count(problem),
        space_is_infinite=discrete_space_size(problem) is None,
        best_value=best,
        best_so_far=best_so_far,
        evaluations_since_improvement=since,
        window_span=span,
        window_indistinguishable=span is not None and span <= assay_noise,
        enough_observations=enough,
        plateaued=enough and since >= span_window,
    )
