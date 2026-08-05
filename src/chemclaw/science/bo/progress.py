"""Whether an optimization is still finding anything, judged against the assay's own noise (W1).

The question this answers is "have we plateaued, or is there more in it?" — asked by a lab leader
who does not want to burn another two weeks. Nothing in the tree computed it: `campaign.optimize`
runs exactly `n_rounds`, and the only early stop is `space_exhausted`, which is discrete-space
exhaustion rather than a plateau.

**`assay_noise` is a required argument with no default, and that is the whole design.** A live
probe was graded *fabricated* for asserting "the last 1-2% gains are real" against a +/-2%
reproducibility the chemist had stated in the same question. A plateau test that supplied its own
default noise would reproduce that error with a tool's authority behind it; one that demands the
number cannot be answered without it.

**A gain is measured from the last real gain, not from the last run** (D-2026-08-05). The first
version compared each result to a continuously updated running best, which meant a campaign
climbing in steps each smaller than the noise never reset the counter no matter how far it climbed:
50.0 -> 70.9 over twelve runs, **+20.9 against a stated +/-2**, reported `plateaued`. That is the
same harm this module exists to prevent, pointing the other way — telling a lab leader to stop a
campaign that is working. The comparison is anchored instead, so cumulative drift counts.

**No BoFire import, deliberately.** Hypervolume would be the textbook multi-objective convergence
metric and `bofire.utils.multiobjective` ships one, but the arithmetic here needs none of it, and
`science.bo.problem` — which this imports — is the campaign job's `params_model` and is loaded in
the agent process, where `tests/test_connector_isolation.py` exists to keep `torch` out.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from chemclaw.core.config import settings
from chemclaw.science.bo.problem import (
    Objective,
    Observation,
    OptimizationProblem,
    discrete_candidate_count,
    distinct_candidate_count,
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
    # Distinct parameter combinations, which is what a design-space efficiency claim divides by:
    # re-running one condition three times is one point of the grid, not three.
    n_distinct: int = Field(ge=0)
    # The full grid size for an all-categorical problem; None when any parameter is continuous and
    # the space is therefore infinite.
    design_space: int | None = None

    best_value: float | None = None
    # The running best after each evaluation, in the order supplied.
    best_so_far: list[float] = Field(default_factory=list)
    # Evaluations since a result beat the value at the **last real gain** by more than
    # `assay_noise`. This is the headline number: it needs no window and it is what "the last real
    # gain was N runs ago" means. Measured against the last real gain rather than against the
    # running best, so a series climbing in sub-noise steps is not called a plateau once the
    # accumulated climb exceeds the noise a chemist would measure between run 1 and run N.
    evaluations_since_improvement: int = Field(default=0, ge=0)

    # Spread of the raw values over the last `window` evaluations — the statement the op-13 grader
    # actually asked for ("the last four results span 87-89 against a stated +/-2%, so they are
    # indistinguishable"). None when there are fewer than two observations to span.
    window_span: float | None = None
    window_indistinguishable: bool = False

    enough_observations: bool = False
    plateaued: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summary(self) -> str:
        """The reading in words, including the limit a plateau verdict may never exceed.

        A `computed_field` rather than a bare property for the reason
        `chemclaw.science.safety.screen.ScreenResult.verdict` is one: a plain property is not
        serialized, so the caveat would never reach the model composing the answer. The tool
        docstring is read once when the tool is defined; this sentence is in the context window at
        the moment the answer is written, and only one of those two is load-bearing.
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
        return (
            f" ({self.n_distinct} distinct condition(s) out of the {self.design_space} "
            "the full grid holds)"
        )


def campaign_progress(
    problem: OptimizationProblem,
    observations: list[Observation],
    assay_noise: float,
    window: int | None = None,
    objective: str | None = None,
) -> CampaignProgress:
    """Read a campaign's observations for a plateau, against the noise the chemist stated.

    Args:
        problem: The decision space and objective the observations belong to.
        observations: The runs so far, **in the order they were performed** — the running best and
            "evaluations since" are both order-dependent, and a set reordered by value would report
            a campaign that never stopped improving.
        assay_noise: The assay's reproducibility, in the objective's own units. Required.
        window: How many recent evaluations the span statement covers, defaulting to
            `bo_plateau_window`.
        objective: Which objective to read. Optional on a single-objective problem, where there is
            only one; **required** on a multi-objective one, where a plateau is per axis and
            picking the lead silently would answer a question nobody asked.

    Returns:
        The reading, with a `summary` stating what it does and does not establish.
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
    # The value at the last *real* gain. The counter measures distance from this anchor rather than
    # from the running best, so a campaign creeping upward in sub-noise steps still registers as
    # improving once the accumulated climb beats the noise. Comparing each run to a continuously
    # updated best made the bar rise underneath the counter, and a monotone climb never reset it:
    # 50.0 -> 70.9 over twelve runs, +20.9 against a stated +/-2, reported "plateaued".
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
        design_space=discrete_candidate_count(problem),
        best_value=best,
        best_so_far=best_so_far,
        evaluations_since_improvement=since,
        window_span=span,
        window_indistinguishable=span is not None and span <= assay_noise,
        enough_observations=enough,
        plateaued=enough and since >= span_window,
    )
