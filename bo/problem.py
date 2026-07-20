"""Framework-neutral specification of a Bayesian-optimization problem (Phase 1d).

These types describe *what* to optimize — continuous and categorical parameters
and a single objective — without any BoFire types. Agents, skills, and the
campaign loop depend only on these; the BoFire mapping is isolated in `bo.engine`
(G6). v1 supports continuous + categorical inputs and one scalar objective;
multi-objective comes when a real problem needs it.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

# A parameter value is a float (continuous) or a category label (categorical).
ParamValue = float | str


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
    """

    params: dict[str, ParamValue]
    value: float
    provenance: str = "measured"


class Candidate(BaseModel):
    """A proposed point to evaluate next."""

    params: dict[str, ParamValue]
