"""Framework-neutral specification of a Bayesian-optimization problem (Phase 1d).

These types describe *what* to optimize — continuous and categorical parameters and one or more
objectives — without any BoFire types. Agents, skills, and the campaign loop depend only on these;
the BoFire mapping is isolated in `chemclaw.science.bo.engine` (G6).

**Multi-objective is inline only** (W3). `suggest_next_experiment` optimizes a trade-off and returns
the Pareto front of the runs it was given; the *durable* campaign refuses one, because
`bo.objectives`'s registry maps a name to a scalar-returning callable and a multi-output registry
would be an abstraction with no real caller.

**Constraints come in two shapes** (W4). `LinearConstraint` expresses the couplings a bound cannot —
"base plus acid under 3 equivalents", a mixture summing to 1 — over continuous parameters, and
`ExcludeConstraint` forbids a *pairing* of two categorical options that are each fine alone. Both
the seeding and the proposing strategy honour them (measured); a factorial screen honours neither,
so `factorial_design` refuses a constrained problem rather than returning runs that violate it.

This module is the campaign job's `params_model`, so it is imported into the agent process — which
`tests/test_connector_isolation.py` keeps `torch` out of. Nothing here may import BoFire, even for
something BoFire ships (`pareto_front` is hand-written for exactly that reason).
"""

from itertools import product
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, computed_field, model_validator

from chemclaw.core.config import settings

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
    """A categorical decision variable — one of a fixed set of labels (e.g. a catalyst).

    Optionally *featurized*: a bare categorical is opaque to the surrogate, which can only
    learn each label independently and therefore cannot say anything about an option that
    has never been run. Giving each category a numeric descriptor vector turns the choice
    into a continuous space the model can interpolate across, so evidence about one ligand
    informs its neighbours (U1). `chemclaw.science.bo.featurize` fills `descriptors` from
    `structures`.

    Both halves are carried so a campaign is auditable and reproducible: `structures` records
    what was featurized, `descriptors` the values the surrogate actually saw. The values live
    in the spec rather than being recomputed per round, so a campaign that crosses the
    Temporal boundary cannot silently change its own featurization mid-run.
    """

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
        """Reject a partial featurization rather than letting BoFire see a ragged matrix (G4).

        Every category needs a structure (if any does) and a descriptor row (if any does),
        and every row needs the same descriptor names in the same order — BoFire's
        `CategoricalDescriptorInput` is a dense matrix, so a missing category or a stray
        descriptor name is a malformed domain, not a partial one.
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
    """A limit the chemist states across *several* parameters at once (W4).

    A limit on one parameter is its bound and belongs there. This type exists for the couplings a
    bound cannot express — "base plus acid must not exceed 3 equivalents", "water is at most 5% of
    the solvent", "these three fractions sum to 1" (which is the mixture/formulation case, and comes
    free as `relation: "=="`).

    **One kind, covering all three relations.** A discriminated union of five constraint types would
    be the single biggest comprehensibility regression available to an LLM-facing schema, and the
    linear family is the only one any continuous story asks for. `kind` discriminates it from the
    one genuinely different shape (`ExcludeConstraint`), so widening later stays additive and never
    changes a linear one's wire shape.

    **Continuous parameters only.** The acquisition optimizer applies constraints to the continuous
    subspace and enumerates the categorical one, and BoFire itself refuses a constraint naming a
    categorical feature (measured) — so the validator here exists to turn a pydantic error into a
    sentence naming the parameter, not to be the safety.
    """

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
    """Two categorical options that must never be combined — "no Pd(OAc)₂ in DMSO" (W4).

    A forbidden *option* is one left out of a category list. This type is for the case a category
    list cannot express: each option is fine on its own and only the *pairing* is forbidden, which
    is how incompatibility usually arrives from a chemist.

    **Exactly two parameters, both categorical**, mirroring `LinearConstraint`'s
    parameter-and-its-value pairing so the two constraint shapes read the same way. BoFire's
    equivalent takes exactly two features, and the `options` lists are ANDed: every pairing in the
    cross product of the two lists is excluded.

    **Only on an all-categorical problem.** Measured: BoFire refuses this constraint on a domain
    that also holds a continuous parameter ("can only be used for pure categorical/discrete search
    spaces"), and it refuses it for a factorial screen outright. Both refusals are re-stated here in
    the caller's own vocabulary, because a pydantic error naming a BoFire class is not something a
    caller can repair from.
    """

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

        The one definition of "excluded", so the space accounting and any later filter cannot drift
        apart the way a re-implemented predicate would.
        """
        return all(
            params.get(name) in set(options)
            for name, options in zip(self.parameters, self.options, strict=True)
        )


# Discriminated union so a serialized constraint round-trips to the right type. Two members, not
# five: the linear family collapses into one shape, and an exclusion is the one thing that genuinely
# is not a linear form.
Constraint = Annotated[LinearConstraint | ExcludeConstraint, Field(discriminator="kind")]


class OptimizationProblem(BaseModel):
    """A full problem: the decision variables, the objective(s), and any cross-parameter limits.

    One `objectives` field rather than a lead objective plus a sidecar list (W3). The sidecar shape
    guarantees that a lone objective sometimes lands in the wrong one, and it bakes a "primary"
    fiction into a Pareto front where every axis is symmetric.
    """

    parameters: list[Parameter] = Field(min_length=1)
    objectives: list[Objective] = Field(min_length=1)
    constraints: list[Constraint] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _accept_the_singular_objective(cls, data: Any) -> Any:
        """Accept `{"objective": {...}}` forever — it is the shape already on disk.

        **Permanent compatibility, not a migration window.** Every `bo_campaigns.problem` row
        written before this change spells the objective that way, and so does every in-flight
        `CampaignSpec` sitting in Temporal history. A validator that rejected the old spelling would
        fail a running campaign at *replay* — the exact hazard `require_rounds_within_ceiling`'s
        comment documents one field over, which is why that rule lives outside this model at all.

        Both spellings together is a caller error rather than a compatibility case: it means the
        writer believed two different things about which objectives the problem has.
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

        A property, deliberately, so it is **not** serialized — the wire shape is `objectives` and
        nothing else. It exists because every reader of the old field wanted `.name` or `.direction`
        (13 of them across six modules), and none of those readings changes when a second objective
        appears.

        The lead objective is privileged in exactly two places, and both are display or identity,
        never optimization: the `bo_campaigns.objective`/`direction` columns, and the legacy half of
        the campaign-id hash. Anything that *optimizes* reads `objectives`, or refuses.
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

        Safe to enforce in the model: every payload written before `objectives` became a list
        carries exactly one objective, so this cannot reject anything already on disk or in a
        Temporal history. The parameter/objective clash check is **not** safe that way and lives
        outside the model — see `require_names_do_not_clash`.
        """
        names = [objective.name for objective in self.objectives]
        if len(names) != len(set(names)):
            raise ValueError("objective names must be unique")
        return self

    @model_validator(mode="after")
    def _constraints_resolve(self) -> "OptimizationProblem":
        """Every constraint names declared parameters, of the kind that constraint can hold.

        BoFire refuses each of these too — a linear form naming a categorical (`Feature solvent is
        not a continuous input feature`), an exclusion on a domain that also holds a continuous
        parameter ("pure categorical/discrete search spaces") — so this validator is about the
        *message*: a pydantic error naming a BoFire internal is not something a caller can repair
        from, and an undeclared parameter would otherwise surface far from the mistake.
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

        The whole-problem condition is the surprising one and belongs on the exclusion that caused
        it: BoFire applies this constraint by enumerating the search space, so a single continuous
        parameter anywhere in the problem makes it unenumerable and the constraint unusable.
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
    """One evaluated point: parameter values and the resulting objective value.

    `provenance` distinguishes a real measurement from a model prediction, so a
    campaign fed by predicted values stays honest about its evidence (D-011).
    `value` must be finite: NaN compares false in both directions, so it would
    silently win `best_of`, and BoFire drops the row mid-campaign — reject it at
    the boundary instead (gate G4).
    """

    params: dict[str, ParamValue]
    # The **lead** objective's value. Unchanged and still required: it is the field every persisted
    # row already carries (`bo_suggestions.observations` JSONB, Temporal history, the `bo-candidate`
    # note), and `values` cannot be reconstructed from a legacy payload — an `Observation` does not
    # know its problem, so `{"value": 0.83}` has no objective name to key on. Symmetry here would
    # have cost four persistence surfaces to buy nothing.
    value: float = Field(allow_inf_nan=False)
    # Objective name -> value, covering **every** objective of a multi-objective problem. Empty for
    # a single-objective one, where `value` says it all. Self-describing on the wire, which is what
    # `resume_campaign` reading a JSONB row back needs.
    values: dict[str, float] = Field(default_factory=dict)
    provenance: str = "measured"
    # The surrogate's posterior sd at this point **when it was proposed** — carried over from the
    # `Candidate`, and deliberately not named `uncertainty`. It does not qualify `value`: `value`
    # came from the evaluator (a calculator, or a real measurement), while this is what the model
    # believed *before* seeing it. Labelling a model's prior spread as the measurement's error
    # would be precisely the overclaim F8-T1 exists to prevent. `None` for a seed point, which had
    # no surrogate behind it.
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
    # The same two quantities per objective, for a multi-objective ask. Empty on a single-objective
    # problem, where the scalars above are the whole answer. Beside the scalars rather than
    # replacing them, for `Observation.value`'s reason: the scalars are what is already persisted.
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

        A `computed_field` rather than a plain property, because a bare property is not serialized
        and the caveat would then never reach the model that asked (the idiom W1 established).
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
    """

    objective: str
    r2: float
    mae: float = Field(ge=0.0)
    folds: int = Field(ge=2)
    n_observations: int = Field(ge=2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summary(self) -> str:
        """The score with both caveats attached, for the reason `Prediction.summary` has one.

        The caveats are not decoration. A cross-validated R² over a campaign's worth of runs is the
        most over-readable number this module produces: it looks like a statement about the
        chemistry, and it is a statement about ten points made by a fit that would give a different
        answer if run again.
        """
        stated = (
            f"Cross-validated on {self.n_observations} run(s) over {self.folds} folds, the "
            f"surrogate for {self.objective!r} predicts held-out runs with R² {self.r2:.2f} and "
            f"mean absolute error {self.mae:.2g}."
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


# What a design's resolution means for the reader, in the terms the reader cares about. Only III and
# IV change how a screen's effects may be read; V and above confound nothing below a three-factor
# interaction, which a screening design is not trying to estimate anyway — hence one shared sentence
# for all of them rather than a row each.
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

    Covers 1–39, which is every resolution a real design can have: resolution is bounded by the
    factor count, and a 40-factor two-level screen is not a thing anyone runs.
    """
    rendered = ""
    for value, symbol in _ROMAN_UNITS:
        while number >= value:
            rendered += symbol
            number -= value
    return rendered


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
    # None = the full grid. An int = a two-level fractional design of that resolution. Below 3 the
    # main effects are confounded with each other, which is not a design anyone can interpret —
    # BoFire refuses to generate one, and so does this field.
    resolution: int | None = Field(default=None, ge=3)
    # Continuous factors screened at their two bounds. Named rather than counted: which factor was
    # collapsed is what a reader needs to know before reading an effect off the screen.
    two_level_continuous: list[str] = Field(default_factory=list)
    # Centre runs added per categorical combination — the rows that detect curvature a two-level
    # design otherwise cannot see. Zero unless asked for, and only meaningful with a continuous
    # factor present.
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

        A `computed_field` rather than a plain property for the reason
        `chemclaw.science.safety.screen.ScreenResult.verdict` is one: a bare property is not
        serialized, so the caveat would never reach the model that has to write the answer. The
        tool docstring is read once when the tool is defined; this sentence is in the context
        window at the moment the answer is composed, and only one of those two is load-bearing.
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
    """A durable BO campaign's configuration (plan step 1d.4).

    `objective_name` names the objective a worker resolves via `chemclaw.science.bo.objectives`; a
    Temporal workflow cannot carry a Python callable across its boundary, so the
    objective is referenced by name and looked up in the evaluate activity.

    **There is no `publish_to_graph` here** (D-157). There used to be, and it was the model-facing
    half of a decision declared twice: the manifest's `publish_to_graph` said the deployment wants
    campaign recommendations reviewed, while this field — default `False`, filled in by the LLM —
    could silently suppress the only permanent artifact a campaign produced. A campaign launched
    without it left no trace at all once Temporal's history expired. Whether a job's knowledge
    reaches the graph is the deployment's call, so it is declared once, in `connector.yaml`, where
    the deployment can see it.
    """

    problem: OptimizationProblem
    objective_name: str = Field(min_length=1)
    # A surrogate needs >=2 seed points (BoFire's floor); batch >=1 per round; rounds may
    # be 0. The config ceiling (`bo_max_rounds`) is deliberately NOT validated here: the
    # spec crosses the Temporal serialization boundary, and a validator reading live config
    # would make an in-flight campaign's own input fail deserialization at replay when the
    # setting is lowered — creation entry points call `require_rounds_within_ceiling` instead.
    n_initial: int = Field(default=5, ge=MIN_SEED_OBSERVATIONS)
    n_rounds: int = Field(default=10, ge=0)
    batch: int = Field(default=1, ge=1)
    # Per-campaign RNG seed so replicate campaigns can vary independently;
    # None means the config default (`settings.bo_seed`), resolved in `bo.engine`.
    seed: int | None = None


def require_rounds_within_ceiling(n_rounds: int) -> None:
    """Reject a round count beyond `bo_max_rounds` — every round costs a real evaluation.

    **Not an event-history bound**, though it was documented as one. The durable campaign re-sends
    its whole observation history to the propose activity every round, so history bytes grow
    quadratically and a campaign well inside this ceiling would have been terminated by the server
    mid-run (measured: round 441 at batch 1). That is fixed where it lives — the workflow
    continues-as-new on the server's own suggestion — not by a number here, which could never
    account for batch size or problem width. What this ceiling refuses is a spec that would spend
    thousands of evaluations, which is a mistake worth catching before the first one is paid for.

    Enforced at campaign/spec *creation* — never inside the `CampaignSpec` model, whose validators
    re-run on deserialization at workflow replay, where a lowered ceiling must not fail an
    in-flight campaign's own input.

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

    Both are dataframe column keys in the frame BoFire is told and asked with, so a collision is a
    silently overwritten column rather than an error: a decision variable and a response sharing a
    name would make the surrogate fit against its own input. Worth refusing.

    **But not from a validator.** Nothing forbade this before `objectives` became a list, so a
    campaign launched earlier may carry such a problem, and `OptimizationProblem`'s validators
    re-run wherever that data is read back — `BoCampaignWorkflow` revalidates its `CampaignSpec` on
    **every replay**, and `read_campaign_thread` revalidates the stored problem on every resume. A
    model-level rule would therefore strand an in-flight campaign at replay and make a stored one
    permanently unreadable, which is the exact hazard `require_rounds_within_ceiling` was moved out
    of the model to avoid. It is enforced where data *enters* instead: the tool boundary and the
    campaign launch.

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

    Featurizing replaces a label with a position in descriptor space, and BoFire's
    `CategoricalDescriptorInput` gives the model *only* that position: the label is gone. Two
    categories at the same position are therefore one point to the surrogate, and it will predict
    one value for both — **measured**, on a two-descriptor parameter whose A and B rows matched:
    with A observed at 10 and B at 90, `predict_at` returned 70.85 for each. No warning, no error;
    a chemist reads a confident recommendation for a reagent the model has never distinguished from
    another.

    Two ways to get here, both plausible. Two category labels pointing at the same SMILES
    (`"Pd(OAc)2"` and `"palladium acetate"`) featurize identically by construction. Or a caller
    supplies `descriptors` directly and repeats a row.

    **Outside the model, for the reason `require_names_do_not_clash` gives**: nothing forbade this
    before, so a stored or in-flight campaign may carry it, and `OptimizationProblem`'s validators
    re-run at workflow replay and on every `resume_campaign` read. A model-level rule would strand
    those rather than refuse a new one.

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


def require_campaign_startable(spec: CampaignSpec) -> None:
    """Every launch-time rule for a durable campaign, in the shape `precondition` is called with.

    One function because `connector.yaml` names exactly one, and `cli/validate_connectors.py` checks
    that the named function accepts the params model — a check that exists because an earlier
    manifest named the round-count rule, which takes an `int`, and every
    `start_optimization_campaign` call raised `TypeError` while CI stayed green.

    Two rules today. The round ceiling is enforced here rather than on `CampaignSpec` because that
    model's validators re-run at workflow *replay*, where a lowered ceiling must not fail an
    in-flight campaign's own input.

    The second is new (W3): **the durable campaign is single-objective**, because
    `bo.objectives`'s registry maps a name to `Callable[..., Awaitable[float]]` — one number per
    evaluation. A multi-output registry would be an abstraction with zero real callers, so the spec
    is refused at launch with a message naming the inline tool that *does* do multi-objective,
    rather than silently optimizing the lead one and reporting a "best" nobody asked for.

    Raises:
        ValueError: When the round count exceeds `bo_max_rounds`, the problem names more than one
            objective, a parameter and an objective share a name, or two categories carry the same
            descriptor row.
    """
    require_rounds_within_ceiling(spec.n_rounds)
    require_names_do_not_clash(spec.problem)
    require_descriptors_distinguish_categories(spec.problem)
    if len(spec.problem.objectives) > 1:
        named = ", ".join(objective.name for objective in spec.problem.objectives)
        raise ValueError(
            f"the durable campaign evaluates one registered objective per round, so it cannot run "
            f"this {len(spec.problem.objectives)}-objective problem ({named}). Use "
            "`suggest_next_experiment`, which does multi-objective inline with a human evaluating "
            "each round, or start a campaign over one of these objectives alone."
        )


class CampaignResult(BaseModel):
    """The outcome of a campaign: the best point found and the full history."""

    best: Observation
    history: list[Observation]


class CampaignCarryOver(BaseModel):
    """What one durable run hands the next when it continues-as-new.

    A campaign's whole mutable state is these two numbers-and-a-list: what has been measured, and
    how many rounds are still owed. Everything else — the problem, the objective name, the batch,
    the seed — is in the `CampaignSpec` the payload already carries and never changes, so it is
    passed through unread rather than copied in here.

    Exists because the round ceiling used to be a promise the workflow could not keep: the history
    is re-sent to the propose activity every round, so event-history bytes grow quadratically and a
    campaign at the configured `bo_max_rounds` would be terminated by the server mid-run, losing
    every already-paid evaluation. Carrying the state across a fresh run resets that growth; the
    carry-over is one list of observations, kilobytes at any round count this ceiling allows.
    """

    history: list[Observation]
    rounds_remaining: int = Field(ge=0)


def observed_value(
    problem: OptimizationProblem, observation: Observation, objective: str | None = None
) -> float:
    """One objective's value off an observation, whichever shape it was given in.

    The single definition of "this observation's number for that objective", so the scalar/vector
    split in `Observation` is read the same way everywhere. `objective=None` means the lead one,
    which on a single-objective problem is the only one.
    """
    name = problem.objective.name if objective is None else objective
    if name in observation.values:
        return observation.values[name]
    if name == problem.objective.name:
        return observation.value
    raise ValueError(
        f"observation reports no value for objective {name!r}; it carries "
        f"{sorted(observation.values) or [problem.objective.name]}"
    )


def require_observations_cover_objectives(
    problem: OptimizationProblem, observations: list[Observation]
) -> None:
    """Every observation reports every objective, and agrees with itself.

    Raised at the tool boundary in the shape `_require_observed_params_match` established, naming
    the observation's index — "which one" is the only useful part of the message.

    On a single-objective problem `values` may be empty (the scalar says it all) or may name exactly
    the one objective. On a multi-objective problem it must cover them all, and `values[lead]` must
    equal `value`, because both are persisted and a reader that trusted the wrong one would report a
    different campaign than the one that ran.

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

    Raises on a multi-objective problem rather than returning the lead objective's winner. There is
    no single best point on a trade-off, and silently picking one axis is exactly the overclaim the
    tool's own instructions forbid: it would report "the best conditions" for a campaign whose whole
    premise is that no such point exists. `pareto_front` is the honest answer.
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

    A per-objective difference of `tolerance` or less is **no difference** in either direction, so
    two runs the assay cannot tell apart never dominate one another.
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

    An observation is on the front when no other is at least as good on **every** objective and
    strictly better on at least one. Order is preserved, so the front reads in the order the runs
    were performed.

    **`tolerance` is the assay's reproducibility, and it defaults to exact.** W1 made `assay_noise`
    required for a plateau verdict because a difference inside the assay is not a difference; a
    front computed at float precision makes the opposite assumption, and would split two runs that
    differ by less than anyone can measure. Passing the number the chemist stated is what makes the
    front a claim about chemistry rather than about floating point.

    The default stays `0.0` rather than becoming required, because the difference between the two
    tools is real: a front without the number is still a true statement about the runs as recorded,
    whereas a plateau verdict without it is not. `ExperimentSuggestion.summary` says which one it
    computed.

    **Pure Python, deliberately not `bofire.utils.multiobjective`.** This module is the campaign
    job's `params_model` and is therefore imported into the agent process, which
    `tests/test_connector_isolation.py` exists to keep `torch` out of. Hypervolume would need
    BoFire; a dominance test needs a comparison.

    Duplicate points both stay: neither dominates the other, and dropping one would quietly discard
    a replicate that is evidence about the assay.
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
    """Distinct candidates in a purely discrete space, or None if it is infinite.

    Any continuous parameter makes the space infinite (returns None). For an
    all-categorical problem it is the product of the category counts — the size at
    which unique-candidate proposals exhaust the space and BoFire's discrete
    acquisition can no longer return a fresh point.

    **An exclusion removes whole cells, so the product over-counts** (W4): a 2×2×2 space minus one
    forbidden catalyst/solvent pairing holds six candidates, not eight. Every caller of this number
    acts on it — the seeding guard refuses `n` above it, and `space_exhausted` decides a campaign is
    finished by it — so an over-count would let a loop keep asking for points that cannot exist.
    The feasible cells are counted by enumeration rather than by inclusion–exclusion, because
    exclusions can overlap and this space is small by construction: it is the space a unique-seeding
    loop already walks one point at a time. The enumeration is skipped entirely when there is
    nothing to exclude, so the cost appears only where the exclusion does.
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
    names = [name for name, _ in counts]
    return sum(
        1
        for cell in product(*(options for _, options in counts))
        if not any(x.forbids(dict(zip(names, cell, strict=True))) for x in exclusions)
    )


def point_in_domain(problem: OptimizationProblem, params: dict[str, ParamValue]) -> bool:
    """Whether every parameter of one point lies inside its declared range or category list (W5).

    Not a validator — a *label*. BoFire does not clamp an out-of-domain point (measured); it
    extrapolates, and the honest answer is the prediction plus the fact that it is an extrapolation.
    A refusal would withhold a number the chemist can read correctly once told which side of the
    bound they are on, so this decides what a `Prediction` says about itself rather than whether one
    exists at all. Constraints are deliberately not consulted: they bound where the *optimizer* may
    propose, and a chemist may legitimately ask what the model expects at a point they cannot run.
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

    The single definition of "same candidate", shared by the exhaustion
    accounting here and the seed deduplication in `chemclaw.science.bo.engine`.
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
