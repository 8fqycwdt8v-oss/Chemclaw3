"""Framework-neutral specification of a Bayesian-optimization problem (Phase 1d).

These types describe *what* to optimize — continuous and categorical parameters
and a single objective — without any BoFire types. Agents, skills, and the
campaign loop depend only on these; the BoFire mapping is isolated in `bo.engine`
(G6). v1 supports continuous + categorical inputs and one scalar objective;
multi-objective comes when a real problem needs it.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from chemclaw.config import settings

# A parameter value is a float (continuous) or a category label (categorical).
ParamValue = float | str

# The fewest observations a surrogate can be fitted on (BoFire's SOBO floor):
# below two experiments the strategy raises mid-campaign, so specs and the
# engine guard against it up front instead (gate G4).
MIN_SEED_OBSERVATIONS = 2


class ContinuousParameter(BaseModel):
    """A continuous decision variable with inclusive bounds."""

    kind: Literal["continuous"] = "continuous"
    name: str = Field(min_length=1)
    lower: float
    upper: float

    @model_validator(mode="after")
    def _bounds_ordered(self) -> "ContinuousParameter":
        """Reject an empty or inverted interval so BoFire never sees a bad domain."""
        if self.lower >= self.upper:
            raise ValueError(f"parameter {self.name!r}: lower must be < upper")
        return self


class CategoricalParameter(BaseModel):
    """A categorical decision variable — one of a fixed set of labels (e.g. a catalyst)."""

    kind: Literal["categorical"] = "categorical"
    name: str = Field(min_length=1)
    categories: list[str] = Field(min_length=2)

    @model_validator(mode="after")
    def _unique_categories(self) -> "CategoricalParameter":
        """Category labels must be distinct."""
        if len(self.categories) != len(set(self.categories)):
            raise ValueError(f"parameter {self.name!r}: categories must be unique")
        return self


# Discriminated union so a serialized problem round-trips to the right parameter type.
Parameter = Annotated[ContinuousParameter | CategoricalParameter, Field(discriminator="kind")]


class Objective(BaseModel):
    """The scalar quantity to optimize, and the direction."""

    name: str = Field(min_length=1)
    direction: Literal["minimize", "maximize"] = "minimize"


class OptimizationProblem(BaseModel):
    """A full problem: the decision variables and the single objective."""

    parameters: list[Parameter] = Field(min_length=1)
    objective: Objective

    @model_validator(mode="after")
    def _unique_names(self) -> "OptimizationProblem":
        """Parameter names must be unique — they are the dataframe column keys."""
        names = [p.name for p in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("parameter names must be unique")
        return self


class Observation(BaseModel):
    """One evaluated point: parameter values and the resulting objective value.

    `provenance` distinguishes a real measurement from a model prediction, so a
    campaign fed by predicted values stays honest about its evidence (D-011).
    `value` must be finite: NaN compares false in both directions, so it would
    silently win `best_of`, and BoFire drops the row mid-campaign — reject it at
    the boundary instead (gate G4).
    """

    params: dict[str, ParamValue]
    value: float = Field(allow_inf_nan=False)
    provenance: str = "measured"


class Candidate(BaseModel):
    """A proposed point to evaluate next."""

    params: dict[str, ParamValue]


class CampaignSpec(BaseModel):
    """A durable BO campaign's configuration (plan step 1d.4).

    `objective_name` names the objective a worker resolves via `bo.objectives`; a
    Temporal workflow cannot carry a Python callable across its boundary, so the
    objective is referenced by name and looked up in the evaluate activity.
    """

    problem: OptimizationProblem
    objective_name: str = Field(min_length=1)
    # A surrogate needs >=2 seed points (BoFire's floor); batch >=1 per round;
    # rounds may be 0 (ceiling checked against config below).
    n_initial: int = Field(default=5, ge=MIN_SEED_OBSERVATIONS)
    n_rounds: int = Field(default=10, ge=0)
    batch: int = Field(default=1, ge=1)
    # Per-campaign RNG seed so replicate campaigns can vary independently;
    # None means the config default (`settings.bo_seed`), resolved in `bo.engine`.
    seed: int | None = None
    # Opt-in: publish the campaign's recommendation as a PR-gated graph note (1d.5).
    publish_to_graph: bool = False

    @model_validator(mode="after")
    def _rounds_within_ceiling(self) -> "CampaignSpec":
        """Reject a round count beyond `bo_max_rounds` — Temporal event history is finite (G4).

        The durable campaign carries its observation history as workflow state and re-sends it
        to the propose activity every round, so history bytes grow quadratically with rounds;
        an unbounded spec would be terminated by the server's hard history limit mid-run,
        losing every already-paid evaluation. Fail at spec time instead, with the knob named.
        """
        if self.n_rounds > settings.bo_max_rounds:
            raise ValueError(
                f"n_rounds={self.n_rounds} exceeds the configured ceiling "
                f"bo_max_rounds={settings.bo_max_rounds}"
            )
        return self


class CampaignResult(BaseModel):
    """The outcome of a campaign: the best point found and the full history."""

    best: Observation
    history: list[Observation]


def best_of(problem: OptimizationProblem, observations: list[Observation]) -> Observation:
    """Return the best observation for the problem's optimization direction."""
    if not observations:
        raise ValueError("no observations")
    best = observations[0]
    for observation in observations[1:]:
        if problem.objective.direction == "minimize":
            improved = observation.value < best.value
        else:
            improved = observation.value > best.value
        if improved:
            best = observation
    return best


def discrete_candidate_count(problem: OptimizationProblem) -> int | None:
    """Distinct candidates in a purely discrete space, or None if it is infinite.

    Any continuous parameter makes the space infinite (returns None). For an
    all-categorical problem it is the product of the category counts — the size at
    which unique-candidate proposals exhaust the space and BoFire's discrete
    acquisition can no longer return a fresh point.
    """
    total = 1
    for parameter in problem.parameters:
        if isinstance(parameter, CategoricalParameter):
            total *= len(parameter.categories)
        else:
            return None
    return total


def params_key(params: dict[str, ParamValue]) -> tuple[tuple[str, ParamValue], ...]:
    """A hashable, order-independent identity for one parameter assignment.

    The single definition of "same candidate", shared by the exhaustion
    accounting here and the seed deduplication in `bo.engine`.
    """
    return tuple(sorted(params.items()))


def distinct_candidate_count(observations: list[Observation]) -> int:
    """How many distinct parameter combinations appear in the observations."""
    return len({params_key(o.params) for o in observations})


def space_exhausted(space: int | None, history: list[Observation], batch: int) -> bool:
    """Whether a purely discrete space is too exhausted to propose a full batch.

    A finite (all-categorical) space runs out of fresh points: once fewer than
    `batch` distinct candidates remain, BoFire's discrete acquisition cannot
    return one and would crash, so a campaign loop must stop cleanly instead.
    `space` is None for an infinite (any-continuous) space, which never exhausts.
    """
    return space is not None and distinct_candidate_count(history) + batch > space
