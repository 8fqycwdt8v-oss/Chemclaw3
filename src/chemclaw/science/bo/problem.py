"""Framework-neutral specification of a Bayesian-optimization problem.

Describes *what* to optimize — continuous and categorical parameters, one or more objectives, and
constraints — with no BoFire types; the BoFire mapping is isolated in `chemclaw.science.bo.engine`.

Multi-objective is inline only: `suggest_next_experiment` returns the Pareto front, while the
durable campaign's registry maps a name to a scalar callable and refuses a trade-off.
`LinearConstraint` couples continuous parameters; `ExcludeConstraint` forbids a pairing of two
categorical options. The seeding and proposing strategies honour both; a factorial screen honours
neither and refuses a constrained problem.

This is the campaign job's `params_model`, imported into the agent process, which must stay free of
`torch`: nothing here may import BoFire (hence the hand-written `pareto_front`).
"""

from itertools import product
from math import isfinite
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, computed_field, model_validator

from chemclaw.core.config import settings

# A parameter value is a float (continuous) or a category label (categorical).
ParamValue = float | str

# The fewest observations a surrogate can be fitted on (BoFire's SOBO floor); specs and the
# engine guard against it up front.
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
    """One of a fixed set of labels — a catalyst, a base, a solvent.

    Give `structures` (label -> SMILES) when the options are molecules: each option is then
    described by computed descriptors instead of as an opaque label, so the surrogate can say
    something about an option nobody has run. `descriptors` is computed from it — leave it unset.
    """

    # Rationale here, not in the docstring: pydantic publishes the docstring as the JSON-schema
    # description, inlined in every tool schema that takes a problem.
    #
    # A descriptor vector per category lets the surrogate interpolate between options instead of
    # learning each label independently (`chemclaw.science.bo.featurize` fills it from
    # `structures`). Both are carried in the spec: `structures` records what was featurized,
    # `descriptors` what the surrogate saw, so featurization cannot change mid-campaign.

    kind: Literal["categorical"] = "categorical"
    name: str = Field(min_length=1)
    categories: list[str] = Field(min_length=2)
    # category label -> SMILES. The declared input to featurization; provenance afterwards.
    structures: dict[str, str] | None = None
    # category label -> {descriptor name: value}. Produced by `bo.featurize.featurize_problem`.
    descriptors: dict[str, dict[str, float]] | None = None

    @model_validator(mode="after")
    def _unique_categories(self) -> "CategoricalParameter":
        """Category labels must be distinct."""
        if len(self.categories) != len(set(self.categories)):
            raise ValueError(f"parameter {self.name!r}: categories must be unique")
        return self

    @model_validator(mode="after")
    def _featurization_is_complete(self) -> "CategoricalParameter":
        """Reject a partial featurization rather than letting BoFire see a ragged matrix.

        If any category has a structure (or descriptor row), all must, and every row needs the same
        descriptor names in the same order: `CategoricalDescriptorInput` is a dense matrix.
        """
        categories = set(self.categories)
        if self.structures is not None and set(self.structures) != categories:
            raise ValueError(
                f"parameter {self.name!r}: structures must cover exactly the categories; "
                f"missing {sorted(categories - set(self.structures))}, "
                f"unexpected {sorted(set(self.structures) - categories)}"
            )
        if self.descriptors is None:
            return self
        if set(self.descriptors) != categories:
            raise ValueError(
                f"parameter {self.name!r}: descriptors must cover exactly the categories; "
                f"missing {sorted(categories - set(self.descriptors))}, "
                f"unexpected {sorted(set(self.descriptors) - categories)}"
            )
        names = [sorted(row) for row in self.descriptors.values()]
        if any(row != names[0] for row in names[1:]):
            raise ValueError(f"parameter {self.name!r}: every category needs the same descriptors")
        if not names[0]:
            raise ValueError(f"parameter {self.name!r}: descriptors must not be empty")
        return self

    def descriptor_names(self) -> list[str]:
        """The descriptor names, in a fixed order, or an empty list when not featurized."""
        if self.descriptors is None:
            return []
        return sorted(next(iter(self.descriptors.values())))


# Discriminated union so a serialized problem round-trips to the right parameter type.
Parameter = Annotated[ContinuousParameter | CategoricalParameter, Field(discriminator="kind")]


class Objective(BaseModel):
    """The scalar quantity to optimize, and the direction."""

    name: str = Field(min_length=1)
    direction: Literal["minimize", "maximize"] = "minimize"


class LinearConstraint(BaseModel):
    """A limit coupling *several* continuous parameters, with one coefficient each.

    "Base plus acid at most 3 equivalents", "water at most 5% of the solvent", "these fractions
    sum to 1" (`relation: "=="`). A limit on one parameter is that parameter's bound, not a
    constraint. Continuous parameters only.
    """

    # Rationale here, not in the docstring (see `CategoricalParameter`). One kind covers `<=`, `>=`
    # and `==`; `kind` discriminates it from `ExcludeConstraint`, so a later widening stays
    # additive. Continuous parameters only: BoFire refuses a constraint naming a categorical, and
    # the validator exists to name the parameter in that error.

    kind: Literal["linear"] = "linear"
    parameters: list[str] = Field(min_length=1)
    coefficients: list[float] = Field(min_length=1)
    relation: Literal["<=", ">=", "=="] = "<="
    rhs: float

    @model_validator(mode="after")
    def _one_coefficient_per_parameter(self) -> "LinearConstraint":
        """A coefficient each, and no parameter named twice."""
        if len(self.parameters) != len(self.coefficients):
            raise ValueError(
                f"constraint over {self.parameters!r} has {len(self.coefficients)} coefficient(s); "
                "give exactly one per parameter"
            )
        if len(set(self.parameters)) != len(self.parameters):
            raise ValueError(f"constraint names a parameter twice: {self.parameters!r}")
        return self

    def describe(self) -> str:
        """The constraint in the chemist's own relation, for a note or a message."""
        terms = " + ".join(
            f"{coefficient:g}·{name}" if coefficient != 1.0 else name
            for name, coefficient in zip(self.parameters, self.coefficients, strict=True)
        )
        return f"{terms} {self.relation} {self.rhs:g}"


class ExcludeConstraint(BaseModel):
    """A pairing of categorical options that must never be combined — "no Pd(OAc)₂ in DMSO".

    Exactly two parameters, both categorical, with an option list each; every pairing in the cross
    product of the two lists is excluded. An option that is bad on its own is simply left out of
    that parameter's category list. Usable only on an all-categorical problem.
    """

    # Rationale here, not in the docstring (see `CategoricalParameter`). For an incompatibility
    # where each option is fine alone and only the pairing is forbidden. Only on an all-categorical
    # problem: BoFire refuses it beside a continuous parameter and in a factorial screen, and the
    # validators restate both refusals in the caller's vocabulary.

    kind: Literal["exclude"] = "exclude"
    parameters: list[str] = Field(min_length=2, max_length=2)
    options: list[list[str]] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def _each_parameter_lists_options(self) -> "ExcludeConstraint":
        """An option list each, non-empty, and no parameter named twice."""
        if self.parameters[0] == self.parameters[1]:
            raise ValueError(f"exclusion names one parameter twice: {self.parameters[0]!r}")
        for name, options in zip(self.parameters, self.options, strict=True):
            if not options:
                raise ValueError(f"exclusion names no option of {name!r}; list at least one")
            if len(set(options)) != len(options):
                raise ValueError(f"exclusion lists an option of {name!r} twice: {options!r}")
        return self

    def describe(self) -> str:
        """The exclusion in the chemist's own words, for a note or a message."""
        sides = [
            f"{name}={'|'.join(options)}"
            for name, options in zip(self.parameters, self.options, strict=True)
        ]
        return f"never {sides[0]} with {sides[1]}"

    def forbids(self, params: dict[str, ParamValue]) -> bool:
        """Whether one parameter assignment is the pairing this excludes.

        The one definition of "excluded", so space accounting and filters cannot drift apart.
        """
        return all(
            params.get(name) in set(options)
            for name, options in zip(self.parameters, self.options, strict=True)
        )


# Discriminated union so a serialized constraint round-trips to the right type.
Constraint = Annotated[LinearConstraint | ExcludeConstraint, Field(discriminator="kind")]


class OptimizationProblem(BaseModel):
    """The decision variables, the objective(s), and any cross-parameter limits."""

    # One `objectives` list rather than a lead objective plus a sidecar: every Pareto axis is
    # symmetric. The docstring stays one line because it is the schema description.

    parameters: list[Parameter] = Field(min_length=1)
    objectives: list[Objective] = Field(min_length=1)
    constraints: list[Constraint] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _accept_the_singular_objective(cls, data: Any) -> Any:
        """Accept `{"objective": {...}}` forever — it is the shape already on disk.

        Stored `bo_campaigns.problem` rows and in-flight `CampaignSpec`s in Temporal history use it,
        and rejecting it would fail a running campaign at replay. Both spellings together is a
        caller error.
        """
        if not isinstance(data, dict) or "objective" not in data:
            return data
        if "objectives" in data:
            raise ValueError("give `objectives`, not both `objective` and `objectives`")
        legacy = dict(data)
        legacy["objectives"] = [legacy.pop("objective")]
        return legacy

    @property
    def objective(self) -> Objective:
        """The lead objective: `objectives[0]`.

        A property so it is not serialized. The lead objective is privileged only for display and
        identity (the `bo_campaigns.objective`/`direction` columns, the legacy campaign-id hash);
        anything that optimizes reads `objectives`, or refuses.
        """
        return self.objectives[0]

    @model_validator(mode="after")
    def _unique_names(self) -> "OptimizationProblem":
        """Parameter names must be unique — they are the dataframe column keys."""
        names = [p.name for p in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("parameter names must be unique")
        return self

    @model_validator(mode="after")
    def _objective_names_are_distinct(self) -> "OptimizationProblem":
        """Objective names are distinct — they are dataframe column keys and would overwrite.

        Safe in the model because every older payload has exactly one objective. The
        parameter/objective clash check is not, and lives in `require_names_do_not_clash`.
        """
        names = [objective.name for objective in self.objectives]
        if len(names) != len(set(names)):
            raise ValueError("objective names must be unique")
        return self

    @model_validator(mode="after")
    def _constraints_resolve(self) -> "OptimizationProblem":
        """Every constraint names declared parameters, of the kind that constraint can hold.

        BoFire refuses these too; this validator exists so the message names the caller's parameter
        rather than a BoFire internal.
        """
        declared = {p.name for p in self.parameters}
        continuous = {p.name for p in self.parameters if isinstance(p, ContinuousParameter)}
        options = {
            p.name: set(p.categories)
            for p in self.parameters
            if isinstance(p, CategoricalParameter)
        }
        for constraint in self.constraints:
            unknown = sorted(set(constraint.parameters) - declared)
            if unknown:
                raise ValueError(
                    f"constraint {constraint.describe()!r} names undeclared parameter(s) "
                    f"{unknown}; this problem declares {sorted(declared)}"
                )
            if isinstance(constraint, LinearConstraint):
                categorical = sorted(set(constraint.parameters) - continuous)
                if categorical:
                    raise ValueError(
                        f"constraint {constraint.describe()!r} names categorical parameter(s) "
                        f"{categorical}. A linear constraint applies to continuous parameters "
                        "only — to forbid a *combination* of two options use an exclusion instead, "
                        "and to forbid one option leave it out of the category list."
                    )
                continue
            self._check_exclusion(constraint, continuous, options)
        return self

    def _check_exclusion(
        self,
        constraint: ExcludeConstraint,
        continuous: set[str],
        options: dict[str, set[str]],
    ) -> None:
        """An exclusion needs two categorical parameters, real options, and no continuous knob.

        BoFire applies the constraint by enumerating the space, so any continuous parameter makes it
        unusable; the error belongs on the exclusion that caused it.
        """
        named_continuous = sorted(set(constraint.parameters) & continuous)
        if named_continuous:
            raise ValueError(
                f"exclusion {constraint.describe()!r} names continuous parameter(s) "
                f"{named_continuous}; an exclusion pairs two *categorical* options. State a "
                "continuous limit as a linear constraint or as that parameter's bounds."
            )
        for name, stated in zip(constraint.parameters, constraint.options, strict=True):
            unknown = sorted(set(stated) - options[name])
            if unknown:
                raise ValueError(
                    f"exclusion {constraint.describe()!r} names option(s) {unknown} that "
                    f"{name!r} does not have; its categories are {sorted(options[name])}"
                )
        if continuous:
            raise ValueError(
                f"exclusion {constraint.describe()!r} needs an all-categorical problem, and this "
                f"one declares continuous parameter(s) {sorted(continuous)}. BoFire applies an "
                "exclusion by enumerating the search space, which a continuous parameter makes "
                "infinite. Fix the continuous parameters to a short list of levels, or drop the "
                "exclusion and reject the forbidden pairing when you read the suggestions."
            )


class Observation(BaseModel):
    """One evaluated point: the parameter values and the objective value they produced.

    With several objectives give every one in `values`. Set `provenance` to `predicted` when the
    number came from a model rather than from the lab.
    """

    # Rationale here, not in the docstring (see `CategoricalParameter`). `provenance` keeps a
    # campaign fed by predicted values honest about its evidence. `value` must be finite: NaN would
    # silently win `best_of`, and BoFire drops the row mid-campaign.

    params: dict[str, ParamValue]
    # The **lead** objective's value, still required: every persisted row carries it, and a legacy
    # payload has no objective name to key `values` on.
    value: float = Field(allow_inf_nan=False)
    # Objective name -> value for every objective of a multi-objective problem; empty for a
    # single-objective one. Finite per value: a NaN reads as "no difference" in `_dominates` and
    # would put an unmeasured run on the Pareto front. A failed measurement is an absent run.
    values: dict[str, Annotated[float, Field(allow_inf_nan=False)]] = Field(default_factory=dict)
    provenance: str = "measured"
    # The surrogate's posterior sd at this point **when it was proposed**, from the `Candidate`. Not
    # named `uncertainty`: it is the model's prior belief, not the error of `value`. `None` for a
    # seed point.
    surrogate_sd: float | None = Field(default=None, ge=0.0)


class Candidate(BaseModel):
    """A proposed point to evaluate next, and what the surrogate believed about it.

    `predicted_value`/`predicted_sd` are the surrogate's posterior mean and standard deviation at
    this point. BoFire returns both from `ask()` — as `<objective>_pred` and `<objective>_sd` — and
    the adapter used to read the parameter columns and drop them, so the optimizer's own statement
    about *why* it proposed this point died one function short of anything that could record it
    (F8-T1 follow-up).

    That statement is the question a chemist asks before spending a week of lab time on a
    recommendation: a small sd is an exploit of a region the model has learned, a large one is an
    excursion into a region it has not, and the recommended value reads identically either way.

    Both are `None` for a design with no surrogate behind it — a space-filling random seed, a
    factorial screen — which is not a missing value but the accurate statement that no model had
    an opinion yet.
    """

    params: dict[str, ParamValue]
    predicted_value: float | None = None
    predicted_sd: float | None = Field(default=None, ge=0.0)
    # The same two quantities per objective for a multi-objective ask; empty on a single-objective
    # problem. Beside the scalars because the scalars are what is persisted.
    predicted_values: dict[str, float] = Field(default_factory=dict)
    predicted_sds: dict[str, float] = Field(default_factory=dict)


class Prediction(BaseModel):
    """What the surrogate believes about a point **the caller** named (W5).

    A separate type from `Candidate` on purpose, holding the same two numbers. A candidate is
    something the optimizer chose and therefore carries an implicit endorsement — run this next.
    A prediction is an answer to a question the chemist asked instead of trusting a recommendation,
    and it endorses nothing. Sharing one type would make the two indistinguishable at the point
    where the difference matters most, which is a summary a human reads before booking lab time.

    `in_domain` is false when any parameter falls outside its declared range or category list.
    Measured: BoFire does **not** clamp such a point — it extrapolates, with the posterior sd
    rising roughly sixfold (1.60/2.60 in range against 16.08 at T=400 on a 20–120 bound). That
    rising sd is an honest signal and a better answer than a refusal, provided the reader is told
    which side of the bound they are on, which is what this flag is for.
    """

    params: dict[str, ParamValue]
    values: dict[str, float]
    sds: dict[str, float]
    in_domain: bool = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summary(self) -> str:
        """The prediction in one sentence, with what it is not.

        A `computed_field` so it is serialized and the caveat reaches the model.
        """
        stated = "; ".join(
            f"{name} {value:.4g} ± {self.sds.get(name, 0.0):.3g}"
            for name, value in sorted(self.values.items())
        )
        answer = (
            f"The model predicts {stated} here. This is an answer about a point you named, not a "
            "recommendation to run it — the optimizer was not asked what to try next."
        )
        if self.in_domain:
            return answer
        return (
            f"{answer} This point is **outside** the declared range, so the model is "
            "extrapolating: nothing constrains the mean, and the widened sd is the only part of "
            "this prediction that is honest about that."
        )


class FitQuality(BaseModel):
    """How well the surrogate behind a recommendation predicts held-out runs (W5).

    Cross-validated on the observations supplied, per objective. `folds` and `n_observations` are
    carried because a score without them cannot be read: R² 0.95 over ten runs and R² 0.95 over two
    hundred are different claims, and only one of them is about the chemistry.

    **These numbers do not reproduce exactly, and are reported to the precision they do reproduce
    to.** BoFire fits the GP's hyperparameters by numerical optimization, and that fit is not
    deterministic — not under a pinned `torch` seed, and not with a fresh copy of the surrogate
    specification. Measured over twelve identical calls on one ten-run problem: R² spanned
    0.906–0.969 and MAE spanned 1.16–1.80, so **MAE varied by more than half its own value**. The
    first version printed R² to three decimals and MAE to three significant figures, which stated a
    stability neither has. Two decimals and two significant figures are what survive a repeat.

    **`r2` is `None` when the held-out runs carry no variance, and that is not a missing number.**
    R² is the fraction of the target's variance the model explains, so with SS_tot = 0 it has no
    denominator — and the library answers 1.0, which this model then published as a perfect fit.
    Measured on eight runs all reading 42: R² 1.0000, MAE 0, three times running. A campaign whose
    assay has flatlined is precisely when a chemist asks whether the surrogate is worth listening
    to, and neither caveat below fires on it, because both are about *precision*. `None` plus the
    sentence `summary` gives is the honest answer: there is nothing here to predict, so no fit
    quality exists. `mae` stays a number — 0 over a constant target is a true statement about the
    predictions, and it is the R² that overclaims.
    """

    objective: str
    r2: float | None = Field(allow_inf_nan=False)
    mae: float = Field(ge=0.0)
    folds: int = Field(ge=2)
    n_observations: int = Field(ge=2)
    # `max - min` over the runs the folds were cut from. R² is scale-free; this is the scale, stated
    # in `summary` in the objective's units so a high R² over a negligible range is visible.
    response_range: float = Field(ge=0.0, allow_inf_nan=False)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summary(self) -> str:
        """The score with both caveats attached, for the reason `Prediction.summary` has one.

        A cross-validated R² over a few runs is easily over-read as a statement about the chemistry.
        """
        if self.r2 is None:
            # Returned whole: there is no score for the caveats below to qualify. The range is
            # stated because the runs may differ by an amount no assay resolves.
            return (
                f"The {self.n_observations} run(s) supplied for {self.objective!r} carry **no "
                f"usable variance** — their whole range is {self.response_range:.2g}, which is "
                "nothing beside the values themselves — so there is nothing for a model to predict "
                "and no fit quality exists. This is a statement about the runs, not about the "
                "surrogate: an assay reading the same number every time is the finding, and it is "
                "usually a dead catalyst, a saturated response or an instrument fault rather "
                "than a flat response surface."
            )
        stated = (
            f"Cross-validated on {self.n_observations} run(s) over {self.folds} folds, the "
            f"surrogate for {self.objective!r} predicts held-out runs with R² {self.r2:.2f} and "
            f"mean absolute error {self.mae:.2g}. Those runs span a response range of "
            f"{self.response_range:.3g} in the objective's own units — R² is a fraction of that "
            "range and says nothing about how large it is."
        )
        repeatability = (
            " Re-running this on the same runs gives a different number — the GP's hyperparameter "
            "fit is not deterministic, and on a ten-run problem R² moved by about 0.06 and MAE by "
            "about half its value across repeats. Do not read a small difference between two of "
            "these scores as a difference between two models."
        )
        if self.n_observations >= settings.bo_fit_quality_trustworthy_observations:
            return stated + repeatability
        return (
            f"{stated} Read it as a sanity check, not as accuracy: with fewer than "
            f"{settings.bo_fit_quality_trustworthy_observations} runs each fold holds out a "
            f"handful of points, so this number moves a lot on one unlucky split.{repeatability}"
        )


# What a design's resolution means for the reader. Only III and IV change how a screen's
# effects may be read; V and above share one sentence.
_CONFOUNDING = {
    3: (
        "Main effects are confounded with two-factor interactions: an effect this screen "
        "attributes to one factor may in fact belong to a pair of the others."
    ),
    4: (
        "Main effects are clear of two-factor interactions, but the two-factor interactions "
        "are confounded with each other."
    ),
}
_HIGH_CONFOUNDING = (
    "Main effects and two-factor interactions are all clear of each other; only three-factor "
    "and higher interactions are confounded."
)

_ROMAN_UNITS = ((10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))


def _roman(number: int) -> str:
    """Render a design resolution the way DoE literature writes it ("resolution IV", not "4").

    Covers 1–39; resolution is bounded by the factor count.
    """
    rendered = ""
    for value, symbol in _ROMAN_UNITS:
        while number >= value:
            rendered += symbol
            number -= value
    return rendered


#: The design criteria this repository exposes, mapped to the BoFire criterion each one builds.
#:
#: A closed set of the criteria whose question is separable in a chemist's terms: estimate the
#: model (D, and A as its common alias), predict across the region (I), or cover the space with no
#: model (space-filling). E, G and K are omitted deliberately.
DESIGN_CRITERIA: tuple[str, ...] = ("d-optimal", "a-optimal", "i-optimal", "space-filling")

#: The model a design is optimal *for*. The same factors and budget give a different design for a
#: linear model than a quadratic one, and a linear-optimal design cannot see curvature. Names are
#: BoFire's own strings.
DESIGN_FORMULAE: tuple[str, ...] = (
    "linear",
    "linear-and-interactions",
    "linear-and-quadratic",
    "fully-quadratic",
)


class OptimalDesign(BaseModel):
    """A model-based or space-filling design: the runs, and what the design cannot do.

    The sibling of `ScreeningDesign` and the answer to the question it refuses. A factorial
    enumerates corners and **honours no constraint**, so a chemist with a real limit ("base plus
    acid under 3 equivalents") and a fixed run budget had nowhere to go: the screen would propose
    combinations the chemistry forbids, and `propose_candidates` answers a different question
    (where to go next given what you have observed) than "lay me out N runs I can start on Monday".

    Three things this carries that the runs alone cannot say, each of which a reader will otherwise
    get wrong:

    - **`formula`** — the model the design is optimal for. Absent for space filling, which assumes
      none. A design built for `linear` is blind to curvature by construction.
    - **`n_terms` against `len(runs)`** — a design with fewer runs than model terms cannot estimate
      that model at all, and BoFire returns one anyway (measured: 3 runs for a 10-term quadratic in
      three factors, no error). `engine.optimal_design` refuses that case; the two numbers are
      carried so a reader can see the margin rather than trust that somebody checked.
    - **`duplicate_runs`** — an optimal design frequently *repeats* a point, because replication at
      an informative corner is what minimises the criterion. Two identical rows are the design
      working, not a bug, and a chemist who deletes the duplicate has changed the design.
    """

    runs: list[dict[str, ParamValue]] = Field(default_factory=list)
    criterion: str = Field(default="d-optimal")
    # `None` for space filling, which is optimal for no model because it assumes none.
    formula: str | None = None
    # Terms in that model, counted by BoFire's own `get_formula_from_string` rather than
    # re-derived here — two definitions of "how many terms" is one that drifts.
    n_terms: int = Field(default=0, ge=0)
    # Rows that repeat an earlier row exactly. Intentional, and named so nobody prunes them.
    duplicate_runs: int = Field(default=0, ge=0)
    # Constraints the domain carried into the design, by name, so the answer can say which limit
    # was honoured rather than claiming generally that limits were.
    honoured_constraints: int = Field(default=0, ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summary(self) -> str:
        """What this design is and is not, in the context window when the answer is written.

        A `computed_field` so it is serialized and reaches the model.
        """
        factors = len(self.runs[0]) if self.runs else 0
        if self.criterion == "space-filling":
            head = (
                f"Space-filling design: {len(self.runs)} run(s) over {factors} factor(s), spread "
                "to cover the region. It assumes no model, so it favours no effect — and it "
                "estimates none either."
            )
        else:
            head = (
                f"A {self.criterion} design for a {self.formula} model: "
                f"{len(self.runs)} run(s) over {factors} factor(s) against {self.n_terms} model "
                f"term(s). Optimal *for that model* — it is blind to any effect the formula omits, "
                f"so a linear design cannot see curvature."
            )
        return " ".join([head, *self._clauses()])

    def _clauses(self) -> list[str]:
        """One sentence per property a reader would otherwise have to infer from the rows."""
        clauses = []
        if self.duplicate_runs:
            clauses.append(
                f"{self.duplicate_runs} run(s) repeat an earlier row exactly. That is the design "
                "working rather than a fault — replication at an informative point is what "
                "minimises the criterion — so run them as written."
            )
        if self.honoured_constraints:
            clauses.append(
                f"{self.honoured_constraints} declared constraint(s) were honoured: every run "
                "below satisfies them, which is what a factorial screen cannot offer."
            )
        clauses.append(
            "It carries no resolution and no alias structure, so unlike a fractional factorial it "
            "cannot tell you which effects are confounded; what it offers instead is that every "
            "run is feasible."
        )
        return clauses


class ScreeningDesign(BaseModel):
    """A screening design — the runs, and an unavoidable statement of *which* design it is (D-092).

    Distinct from `Candidate`/BO's one-batch-at-a-time proposals: this is a complete, up-front
    design a human runs as a batch, generated by `chemclaw.science.bo.engine.factorial_design`.

    `resolution` is `None` for the full grid and an integer for a reduced (fractional) one. It is
    the field that makes a reduced design safe to return at all: a fractional design *looks* like a
    smaller full grid, and a reader who is not told otherwise will read 16 rows over 7 factors as
    "every combination that matters". The runs alone cannot say which of the two they are.

    `two_level_continuous` is the same idea one level down (W2). A continuous factor admitted to a
    screen is held at its two **bounds** — a temperature column reading 20 and 120 and nothing
    between them is a two-level encoding of a range, not a decision that those are the interesting
    temperatures, and it looks identical to a deliberate pair of levels.
    """

    runs: list[dict[str, ParamValue]] = Field(default_factory=list)
    # None = the full grid; an int = a two-level fractional design of that resolution. Below 3 main
    # effects are confounded with each other.
    resolution: int | None = Field(default=None, ge=3)
    # Continuous factors screened at their two bounds. Named rather than counted: which factor was
    # collapsed is what a reader needs to know before reading an effect off the screen.
    two_level_continuous: list[str] = Field(default_factory=list)
    # Centre runs per categorical combination, which detect curvature; only meaningful with a
    # continuous factor.
    n_center: int = Field(default=0, ge=0)
    # How many times the factorial part is replicated. Replication is what gives a screen a
    # pure-error estimate; without it no effect the screen reports has a significance.
    n_repetitions: int = Field(default=1, ge=1)
    # Whether the run order was shuffled, so a drift over the day is not read as a factor effect.
    randomized: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summary(self) -> str:
        """What this design is, in one sentence the model cannot answer around.

        A `computed_field` so it is serialized and present when the answer is composed.
        """
        factors = len(self.runs[0]) if self.runs else 0
        if self.resolution is None:
            head = (
                f"Full factorial over {factors} factor(s), {len(self.runs)} run(s) in total. "
                "Exhaustive over the levels stated: every combination of them is run."
            )
        else:
            confounding = _CONFOUNDING.get(self.resolution, _HIGH_CONFOUNDING)
            head = (
                f"Fractional factorial, resolution {_roman(self.resolution)}: {len(self.runs)} "
                f"run(s) against the {2**factors} a full two-level grid over {factors} factors "
                f"would need. NOT exhaustive — most combinations are deliberately not run. "
                f"{confounding}"
            )
        return " ".join([head, *self._design_clauses()])

    def _design_clauses(self) -> list[str]:
        """One sentence per non-default choice, so nothing about the design is left implicit."""
        clauses = []
        if self.two_level_continuous:
            named = ", ".join(self.two_level_continuous)
            clauses.append(
                f"{named} {'is' if len(self.two_level_continuous) == 1 else 'are'} continuous and "
                "held at the two ends of the declared range — this screen says nothing about what "
                "happens between them."
            )
        if self.n_center:
            clauses.append(
                f"{self.n_center} centre run(s) per combination of the categorical factors sit at "
                "the midpoint of every continuous factor; they are what would reveal curvature a "
                "two-level design cannot otherwise see."
            )
        if self.n_repetitions > 1:
            clauses.append(
                f"The factorial part is replicated {self.n_repetitions} times, which is what gives "
                "the screen a pure-error estimate to judge an effect against."
            )
        if self.randomized:
            clauses.append(
                "Run order is randomized, so a drift over the session is not read as a factor "
                "effect — run them in the order given."
            )
        return clauses


class CampaignSpec(BaseModel):
    """A durable BO campaign's configuration: the problem, the objective, and the budget.

    `objective_name` names the objective a worker resolves via `chemclaw.science.bo.objectives`.
    """

    # Rationale here, not in the docstring (see `CategoricalParameter`). A Temporal workflow cannot
    # carry a callable, so the objective is referenced by name. Whether a campaign publishes to the
    # graph is the deployment's call (`connector.yaml`), not a field here.

    problem: OptimizationProblem
    objective_name: str = Field(min_length=1)
    # A surrogate needs >=2 seed points; batch >=1; rounds may be 0. `bo_max_rounds` is not
    # validated here: these validators re-run at replay, and a lowered setting must not fail an
    # in-flight campaign; creation calls `require_rounds_within_ceiling` instead.
    n_initial: int = Field(default=5, ge=MIN_SEED_OBSERVATIONS)
    n_rounds: int = Field(default=10, ge=0)
    batch: int = Field(default=1, ge=1)
    # Per-campaign RNG seed so replicate campaigns can vary independently;
    # None means the config default (`settings.bo_seed`), resolved in `bo.engine`.
    seed: int | None = None


def require_rounds_within_ceiling(n_rounds: int) -> None:
    """Reject a round count beyond `bo_max_rounds`.

    Bounds campaign length, not cost (rounds times batch, which `require_evaluations_within_budget`
    bounds); history growth is handled by the workflow's continue-as-new. Enforced at creation,
    never in the `CampaignSpec` model, whose validators re-run at replay.

    Raises:
        ValueError: When `n_rounds` exceeds the configured `bo_max_rounds`.
    """
    if n_rounds > settings.bo_max_rounds:
        raise ValueError(
            f"n_rounds={n_rounds} exceeds the configured ceiling "
            f"bo_max_rounds={settings.bo_max_rounds}"
        )


def require_names_do_not_clash(problem: OptimizationProblem) -> None:
    """No parameter and objective share a name — checked *outside* the model, deliberately.

    Both are dataframe column keys, so a clash would make the surrogate fit against its own input.
    Not a validator, because the model revalidates at workflow replay and on every resume, and a
    rule older data violates would strand it; enforced at the tool boundary and campaign launch
    instead.

    Raises:
        ValueError: Naming the clashing name(s).
    """
    clashes = sorted({o.name for o in problem.objectives} & {p.name for p in problem.parameters})
    if clashes:
        raise ValueError(
            f"{clashes} is both a parameter and an objective; names must differ. They are the same "
            "dataframe column to the surrogate, so one would silently overwrite the other."
        )


def require_descriptors_distinguish_categories(problem: OptimizationProblem) -> None:
    """No two categories may carry the same descriptor row — the surrogate cannot tell them apart.

    `CategoricalDescriptorInput` gives the model only the descriptor position, so identical rows
    (e.g. two labels for one SMILES) get one prediction. Outside the model for the reason
    `require_names_do_not_clash` gives.

    Raises:
        ValueError: Naming the parameter and the categories that collide.
    """
    for parameter in problem.parameters:
        if not isinstance(parameter, CategoricalParameter) or parameter.descriptors is None:
            continue
        seen: dict[tuple[tuple[str, float], ...], str] = {}
        for category, row in parameter.descriptors.items():
            key = tuple(sorted(row.items()))
            if key in seen:
                raise ValueError(
                    f"parameter {parameter.name!r}: categories {seen[key]!r} and {category!r} have "
                    "identical descriptors, so the surrogate sees one point where you named two "
                    "and will report the same prediction for both. Drop one, or give them "
                    "descriptors that actually differ."
                )
            seen[key] = category


def require_direction_matches_objective(spec: CampaignSpec) -> None:
    """The declared direction must be the one its registered objective is actually better in.

    The `objectives` import is deferred because that module imports this one. The check lives here
    because `connector.yaml` names one `precondition`, reachable from this module. An unknown name
    raises from `registered_direction` with the known names.

    Raises:
        ValueError: Naming the objective, both directions, and which one to change.
    """
    from chemclaw.science.bo.objectives import is_measured, registered_direction

    # A measured objective has no registered direction; the requester's statement is the only one,
    # so skip rather than compare against an invented default.
    if is_measured(spec.objective_name):
        return

    declared = spec.problem.objective.direction
    registered = registered_direction(spec.objective_name)
    if declared != registered:
        raise ValueError(
            f"this campaign declares `direction={declared!r}` for objective "
            f"{spec.problem.objective.name!r}, but the registered objective "
            f"{spec.objective_name!r} is {registered}d — so the search would run backwards and "
            f"report its worst point as the best one. Set the problem's direction to "
            f"{registered!r}, or start a campaign over an objective that is {declared}d."
        )


def require_problem_supplies_what_the_objective_reads(spec: CampaignSpec) -> None:
    """The decision space must declare every parameter the registered objective reads.

    Otherwise the campaign fails at evaluate time, after the seed round, with a bare `KeyError`. A
    measured objective is skipped. Ranges are not checked: a fitted emulator extrapolates flat
    outside its training data, which this does not guard.

    Raises:
        ValueError: Naming the objective, the parameters it reads, and the ones the space lacks.
    """
    from chemclaw.science.bo.objectives import is_measured, registered_parameters

    if is_measured(spec.objective_name):
        return
    required = registered_parameters(spec.objective_name)
    declared = {parameter.name for parameter in spec.problem.parameters}
    missing = [name for name in required if name not in declared]
    if missing:
        raise ValueError(
            f"the registered objective {spec.objective_name!r} reads {list(required)} from every "
            f"candidate, but this decision space declares {sorted(declared)} — so it would fail "
            f"at evaluate time on {missing}, after the seed round had been paid for. Declare the "
            "missing parameter(s), or start a campaign over an objective this space fits."
        )


def require_problem_yields_one_best_point(problem: OptimizationProblem) -> None:
    """Every rule a loop that returns a single best observation needs of its problem.

    One statement of the rule for both callers. A trade-off has no single best point, so a
    multi-objective problem is refused before any evaluation budget is spent.

    Raises:
        ValueError: When a parameter and an objective share a name, two categories carry identical
        descriptors, or the problem names more than one objective.
    """
    require_names_do_not_clash(problem)
    require_descriptors_distinguish_categories(problem)
    if len(problem.objectives) > 1:
        named = ", ".join(objective.name for objective in problem.objectives)
        raise ValueError(
            f"this campaign returns one best point and this problem has "
            f"{len(problem.objectives)} objectives ({named}), which have no single best point — a "
            "trade-off has a front, not a winner. Optimize one of them alone, or use the inline "
            "`suggest_next_experiment`, which does multi-objective and returns the Pareto front of "
            "the runs it is given."
        )


def require_evaluations_within_budget(spec: CampaignSpec) -> None:
    """Reject a spec whose whole evaluation budget exceeds `bo_max_evaluations`.

    The budget is seed points plus every round's batch — how many times the objective is actually
    called; the round ceiling alone does not bound it. Enforced at creation, never on the model,
    which revalidates at replay.

    Raises:
        ValueError: When the spec's total evaluation budget exceeds `bo_max_evaluations`.
    """
    budget = spec.n_initial + spec.n_rounds * spec.batch
    if budget > settings.bo_max_evaluations:
        raise ValueError(
            f"this campaign would run {budget} objective evaluation(s) "
            f"(n_initial={spec.n_initial} + n_rounds={spec.n_rounds} x batch={spec.batch}), "
            f"beyond the configured ceiling of {settings.bo_max_evaluations}. Every one is a real "
            "measurement or calculation, so the cost is paid whether or not the campaign converges "
            "— reduce the rounds or the batch, or raise `CHEMCLAW_BO_MAX_EVALUATIONS` if this "
            "deployment genuinely has that budget."
        )


def require_campaign_startable(spec: CampaignSpec) -> None:
    """Every launch-time rule for a durable campaign, in the shape `precondition` is called with.

    One function because `connector.yaml` names exactly one and `cli/validate_connectors.py` checks
    it accepts the params model. Not on `CampaignSpec`, whose validators re-run at replay. The
    durable campaign is single-objective (its registry returns one number per evaluation); the
    direction and parameter checks catch a campaign that would run to completion and answer wrongly.

    Raises:
        ValueError: When the round count exceeds `bo_max_rounds`, the total evaluation budget
        exceeds `bo_max_evaluations`, the problem names more than one objective, a parameter and an
        objective share a name, two categories carry the same descriptor row, the declared direction
        disagrees with the registered objective's, or the decision space omits a parameter that
        objective reads.
    """
    require_rounds_within_ceiling(spec.n_rounds)
    require_evaluations_within_budget(spec)
    require_problem_yields_one_best_point(spec.problem)
    require_direction_matches_objective(spec)
    require_problem_supplies_what_the_objective_reads(spec)


class CampaignResult(BaseModel):
    """The outcome of a campaign: the best point found and the full history."""

    best: Observation
    history: list[Observation]


class CampaignCarryOver(BaseModel):
    """What one durable run hands the next when it continues-as-new.

    A campaign's whole mutable state is these numbers-and-a-list: what has been measured, how many
    rounds are still owed, and how many have been run. Everything else — the problem, the objective
    name, the batch, the seed — is in the `CampaignSpec` the payload already carries and never
    changes, so it is passed through unread rather than copied in here.

    `rounds_done` is carried for one reason: the per-round campaign-record write keys its
    idempotency on `(campaign_id, job_id)` with the round index in the `job_id`, so a continued run
    that restarted the count would write rows colliding with the previous run's and lose them to
    the `ON CONFLICT DO NOTHING`. It defaults to 0 so a carry-over written by an older worker
    mid-upgrade still deserializes.

    Exists because the round ceiling used to be a promise the workflow could not keep: the history
    is re-sent to the propose activity every round, so event-history bytes grow quadratically and a
    campaign at the configured `bo_max_rounds` would be terminated by the server mid-run, losing
    every already-paid evaluation. Carrying the state across a fresh run resets that growth; the
    carry-over is one list of observations, kilobytes at any round count this ceiling allows.

    `spent_seconds` is carried for a reason of the same shape one layer out. The ceiling a campaign
    runs under is a workflow *execution* timeout, which spans the whole continue-as-new chain, while
    `workflow.info().workflow_start_time` is the run's own start and resets on every continuation.
    A continued run that measured only its own elapsed time would believe it had the full budget
    again and hand its last dispatch a queue wait the ceiling cannot honour. Nothing on
    `workflow.info()` carries the chain's start, so the campaign carries it. It defaults to 0.0 so a
    carry-over written by an older worker mid-upgrade still deserializes — and a run that
    deserializes one under-counts what it has spent, which errs towards a longer wait exactly once
    before the next continuation writes the field.
    """

    history: list[Observation]
    rounds_remaining: int = Field(ge=0)
    rounds_done: int = Field(default=0, ge=0)
    spent_seconds: float = Field(default=0.0, ge=0)


def observed_value(
    problem: OptimizationProblem, observation: Observation, objective: str | None = None
) -> float:
    """One objective's value off an observation, whichever shape it was given in.

    The single reading of the scalar/vector split in `Observation`; `objective=None` means the lead
    one. Also refuses a non-finite number, since `values` can be mutated past its validator and
    every reader (dominance, plateau, the BoFire frame) comes through here.

    Raises:
        ValueError: When the observation reports no value for `objective`, or reports one that is
        not a finite number.
    """
    name = problem.objective.name if objective is None else objective
    if name in observation.values:
        return _require_finite(observation.values[name], name, observation)
    if name == problem.objective.name:
        return _require_finite(observation.value, name, observation)
    raise ValueError(
        f"observation reports no value for objective {name!r}; it carries "
        f"{sorted(observation.values) or [problem.objective.name]}"
    )


def _require_finite(value: float, objective: str, observation: Observation) -> float:
    """Return `value`, or raise naming the objective and the run it belongs to.

    The run is named by its parameters, since not every caller enumerates a list.
    """
    if not isfinite(value):
        raise ValueError(
            f"the run at {observation.params!r} reports {value!r} for objective {objective!r}. A "
            "failed or unmeasured assay is an absent run, not a run with a non-finite number: "
            "leave the run out, or supply the measurement."
        )
    return value


def require_observations_cover_objectives(
    problem: OptimizationProblem, observations: list[Observation]
) -> None:
    """Every observation reports every objective, and agrees with itself.

    On a single-objective problem `values` may be empty or name exactly that objective. On a
    multi-objective one it must cover all, and `values[lead]` must equal `value`, since both are
    persisted.

    Raises:
        ValueError: Naming the offending observation's index and what it is missing.
    """
    declared = [objective.name for objective in problem.objectives]
    lead = declared[0]
    for index, observation in enumerate(observations):
        if not observation.values:
            if len(declared) > 1:
                raise ValueError(
                    f"observations[{index}] reports one value, but this problem has "
                    f"{len(declared)} objectives {declared} — give `values` naming each one."
                )
            continue
        missing = sorted(set(declared) - set(observation.values))
        undeclared = sorted(set(observation.values) - set(declared))
        if missing or undeclared:
            raise ValueError(
                f"observations[{index}] reports {sorted(observation.values)} but the problem "
                f"declares {declared}; every observation must give a value for exactly the "
                "objectives the problem declares."
            )
        tolerance = 1e-9 * max(1.0, abs(observation.value))
        if abs(observation.values[lead] - observation.value) > tolerance:
            raise ValueError(
                f"observations[{index}] disagrees with itself: `value` is {observation.value!r} "
                f"but `values[{lead!r}]` is {observation.values[lead]!r}. `value` is the lead "
                "objective's number and both are stored, so they cannot differ."
            )


def best_of(problem: OptimizationProblem, observations: list[Observation]) -> Observation:
    """Return the best observation for a **single-objective** problem's direction.

    Raises on a multi-objective problem: a trade-off has no single best point; use `pareto_front`.
    """
    if not observations:
        raise ValueError("no observations")
    if len(problem.objectives) > 1:
        named = ", ".join(objective.name for objective in problem.objectives)
        raise ValueError(
            f"this problem has {len(problem.objectives)} objectives ({named}), so there is no "
            "single best observation — call `pareto_front` for the non-dominated set"
        )
    best = observations[0]
    for observation in observations[1:]:
        if problem.objective.direction == "minimize":
            improved = observation.value < best.value
        else:
            improved = observation.value > best.value
        if improved:
            best = observation
    return best


def _dominates(
    problem: OptimizationProblem, a: Observation, b: Observation, tolerance: float = 0.0
) -> bool:
    """Whether `a` is at least as good as `b` everywhere and strictly better somewhere.

    A per-objective difference of `tolerance` or less is **no difference** in either direction.
    """
    at_least_as_good = True
    strictly_better = False
    for objective in problem.objectives:
        left = observed_value(problem, a, objective.name)
        right = observed_value(problem, b, objective.name)
        gain = left - right if objective.direction == "maximize" else right - left
        if gain < -tolerance:
            at_least_as_good = False
            break
        if gain > tolerance:
            strictly_better = True
    return at_least_as_good and strictly_better


def pareto_front(
    problem: OptimizationProblem, observations: list[Observation], tolerance: float = 0.0
) -> list[Observation]:
    """The non-dominated observations: the trade-off the runs actually show.

    An observation is on the front when no other is at least as good on every objective and strictly
    better on one. Order is preserved; duplicates both stay (a replicate is evidence about the
    assay).

    `tolerance` is the assay's reproducibility; pass the chemist's stated number so the front does
    not split runs nobody can tell apart. It defaults to exact because a front over recorded values
    is still true without it. Pure Python: this module must not import BoFire.
    """
    if tolerance < 0:
        raise ValueError(
            f"tolerance is an assay reproducibility and cannot be negative; got {tolerance}"
        )
    return [
        candidate
        for candidate in observations
        if not any(_dominates(problem, other, candidate, tolerance) for other in observations)
    ]


def discrete_candidate_count(problem: OptimizationProblem) -> int | None:
    """Distinct candidates in a purely discrete space, or None if it is infinite or too large.

    Any continuous parameter returns None. Otherwise the product of category counts, minus cells an
    exclusion forbids (counted by enumeration, since exclusions can overlap; skipped with no
    exclusions). Callers act on it — seeding refuses `n` above it, `space_exhausted` stops on it —
    so it must not over-count. The enumeration is bounded by `bo_max_enumerated_cells`; above that
    the answer is `None` ("effectively unbounded"), which callers already handle.
    """
    counts: list[tuple[str, list[str]]] = []
    total = 1
    for parameter in problem.parameters:
        if isinstance(parameter, CategoricalParameter):
            counts.append((parameter.name, list(parameter.categories)))
            total *= len(parameter.categories)
        else:
            return None
    exclusions = [c for c in problem.constraints if isinstance(c, ExcludeConstraint)]
    if not exclusions:
        return total
    # The cost is cells times exclusions. This runs synchronously in `campaign_progress` and on the
    # workflow thread at every replay, where Temporal's workflow-task timeout applies.
    if total * len(exclusions) > settings.bo_max_enumerated_cells:
        return None
    names = [name for name, _ in counts]
    return sum(
        1
        for cell in product(*(options for _, options in counts))
        if not any(x.forbids(dict(zip(names, cell, strict=True))) for x in exclusions)
    )


def discrete_space_size(problem: OptimizationProblem) -> int | None:
    """The product of the category counts — the grid *before* any exclusion removes cells.

    `None` means only that a continuous parameter makes the space infinite, unlike
    `discrete_candidate_count`. Cheap at any magnitude. Seeding must use this, or a merely large
    space would take the infinite-space branch and lose deduplication and the `n` check.
    """
    total = 1
    for parameter in problem.parameters:
        if not isinstance(parameter, CategoricalParameter):
            return None
        total *= len(parameter.categories)
    return total


def point_is_feasible(problem: OptimizationProblem, params: dict[str, ParamValue]) -> bool:
    """Whether one point is a cell `discrete_candidate_count` would actually count.

    Inside the declared domain and not forbidden by any exclusion — the enumeration's two filters.
    Unlike `point_in_domain`, which labels extrapolation and ignores constraints.
    """
    if not point_in_domain(problem, params):
        return False
    return not any(
        constraint.forbids(params)
        for constraint in problem.constraints
        if isinstance(constraint, ExcludeConstraint)
    )


def point_in_domain(problem: OptimizationProblem, params: dict[str, ParamValue]) -> bool:
    """Whether every parameter of one point lies inside its declared range or category list.

    A label, not a validator: BoFire extrapolates out-of-domain points, and a `Prediction` says so
    rather than being withheld. Constraints are not consulted, since a chemist may ask about a point
    they cannot run.
    """
    for parameter in problem.parameters:
        value = params.get(parameter.name)
        if isinstance(parameter, ContinuousParameter):
            if not isinstance(value, int | float) or not (
                parameter.lower <= float(value) <= parameter.upper
            ):
                return False
        elif value not in parameter.categories:
            return False
    return True


def params_key(params: dict[str, ParamValue]) -> tuple[tuple[str, ParamValue], ...]:
    """A hashable, order-independent identity for one parameter assignment.

    The single definition of "same candidate", shared with seed deduplication in the engine.
    """
    return tuple(sorted(params.items()))


def distinct_candidate_count(observations: list[Observation]) -> int:
    """How many distinct parameter combinations appear in the observations.

    What was *run*, unfiltered by feasibility; space accounting uses
    `distinct_feasible_candidate_count`.
    """
    return len({params_key(o.params) for o in observations})


def distinct_feasible_candidate_count(
    problem: OptimizationProblem, observations: list[Observation]
) -> int:
    """How many distinct observed points occupy a cell of the *feasible* design space.

    The history must be counted under the same feasibility filter as `discrete_candidate_count`, or
    a run of a later-excluded pairing consumes a cell it was never part of and campaigns are
    declared exhausted early.
    """
    return len({params_key(o.params) for o in observations if point_is_feasible(problem, o.params)})


def space_exhausted(
    problem: OptimizationProblem, space: int | None, history: list[Observation], batch: int
) -> bool:
    """Whether a purely discrete space is too exhausted to propose a full batch.

    Once fewer than `batch` fresh candidates remain BoFire's discrete acquisition would crash, so a
    loop stops cleanly. `space` is None for an infinite space, which never exhausts. `problem` is
    taken so the history is counted under the same feasibility filter as `space`.
    """
    return space is not None and distinct_feasible_candidate_count(problem, history) + batch > space
