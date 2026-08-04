"""Framework-neutral specification of a Bayesian-optimization problem (Phase 1d).

These types describe *what* to optimize — continuous and categorical parameters
and a single objective — without any BoFire types. Agents, skills, and the
campaign loop depend only on these; the BoFire mapping is isolated in `chemclaw.science.bo.engine`
(G6). v1 supports continuous + categorical inputs and one scalar objective;
multi-objective comes when a real problem needs it.
"""

from typing import Annotated, Literal

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
    """Reject a round count beyond `bo_max_rounds` — Temporal event history is finite (G4).

    The durable campaign carries its observation history as workflow state and re-sends it
    to the propose activity every round, so history bytes grow quadratically with rounds;
    an unbounded round count would be terminated by the server's hard history limit mid-run,
    losing every already-paid evaluation. Enforced at campaign/spec *creation* — never inside
    the `CampaignSpec` model, whose validators re-run on deserialization at workflow replay,
    where a lowered ceiling must not fail an in-flight campaign's own input.

    Raises:
        ValueError: When `n_rounds` exceeds the configured `bo_max_rounds`.
    """
    if n_rounds > settings.bo_max_rounds:
        raise ValueError(
            f"n_rounds={n_rounds} exceeds the configured ceiling "
            f"bo_max_rounds={settings.bo_max_rounds}"
        )


def require_campaign_within_ceiling(spec: CampaignSpec) -> None:
    """The same ceiling, in the shape a declared `precondition` is called with.

    `JobSpec.precondition` is documented as taking *the validated params object*, and
    `connectors/jobs.py` calls it that way. `connectors/bo/connector.yaml` named the function above
    instead, which takes an `int`, so `start_optimization_campaign` raised
    `TypeError: '>' not supported between instances of 'CampaignSpec' and 'int'` on every call —
    the reference connector's flagship job could not be started at all, while CI stayed green
    because the only tests call the ceiling rule with a bare int.

    Two entry points with two shapes, so this is an adapter rather than a widened signature: the
    creation path in `bo.campaign` genuinely holds a round count and nothing else.
    """
    require_rounds_within_ceiling(spec.n_rounds)


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
