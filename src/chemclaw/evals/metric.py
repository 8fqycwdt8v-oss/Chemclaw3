"""Metric interface + registry — the evaluation layer's core (plan step 2b.1).

(Singular `metric` = the interface and `@metric` registry. The concrete scored metrics live
in the sibling `chemclaw.evals.metrics` — plural. Import the registry from here, the functions
there.)

Why this layer exists: the Checkmates gate *code* quality, but scientific *output*
quality needs its own measurable gate (docs/archive/research-review.md F7-F9). A metric is a
**pure function** from an evaluation case to a `MetricResult` — value plus provenance
and an optional pass/fail against a config threshold (never a hardcoded one, G3).

The registry is the extension seam for plan step 2b.5: every later capability phase
registers >=1 scientific metric with `@metric(name, direction)`, and a regression in a registered
metric is treated like a failing test. Registration happens on import, so
`evals/__init__.py` imports the seed-metric module to populate the registry.
"""

from collections.abc import Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.errors import ChemclawError


class Direction(StrEnum):
    """Which way a metric's value has to move to be *better* news.

    Registered beside the metric because the value alone cannot say: 0.9 is a good `f1` and a bad
    `prediction_error`, and half the metrics here are ungated (`passed is None`), so the pass
    threshold — the only other place a direction is implied — does not exist for them. Anything
    that compares two runs of the same metric (the baseline comparison in `evals.baseline`) needs
    this to tell an improvement from a regression, and guessing the sign is exactly the
    silently-wrong-answer failure `bo_regret`'s required `output.direction` already refuses to make.
    """

    HIGHER_IS_BETTER = "higher"
    LOWER_IS_BETTER = "lower"


class MetricResult(BaseModel):
    """One metric's verdict on one case: the value and everything needed to cite it.

    `passed` is `None` for a progress/diagnostic metric that has no pass threshold
    (e.g. regret), and a bool for a metric gated against a config limit. `provenance`
    states how the number was derived so a report row stands on its own (G5).
    """

    metric: str = Field(min_length=1)
    value: float
    unit: str | None = None
    passed: bool | None = None
    uncertainty: float | None = Field(default=None, ge=0.0)
    provenance: str = Field(min_length=1)


class EvalCase(BaseModel):
    """One versioned evaluation case: the output under test and its ground truth.

    `output` is the produced result to score; `reference` is the held-out truth a
    metric compares against (absent for metrics computed from the output alone, such
    as green-chemistry mass metrics). `metrics` names the registered metrics to run,
    so a single case can be scored by several of them.

    Extra top-level keys are rejected (not silently dropped): a misspelled field like
    `outputt`, or a `direction` placed at the case root instead of under `output`,
    would otherwise vanish and yield a silently wrong score (G4).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    metrics: list[str] = Field(min_length=1)
    output: dict[str, Any] = Field(default_factory=dict)
    reference: dict[str, Any] | None = None
    # Whether this case's gated metrics are *supposed* to pass. Two cases in the shipped set exist
    # precisely to demonstrate a gate firing — a solvent-heavy step that must exceed the PMI limit,
    # a query whose literal match must miss — and without this field their failure is
    # indistinguishable from a regression. That is why `make eval` could never gate on a science
    # regression despite `ci.yml` calling it "the scientific quality gates": the only way to keep a
    # demonstration case from failing the command was for the command never to fail at all.
    #
    # Declared per case rather than inferred, and defaulting to True, so a *new* case is gated
    # unless someone deliberately says otherwise.
    expect_pass: bool = True


class MetricError(ChemclawError):
    """A metric could not be computed for a case (missing/invalid inputs, G4)."""


# A metric is a pure function: it reads a case and returns its scored result.
Metric = Callable[[EvalCase], MetricResult]

_REGISTRY: dict[str, Metric] = {}
_DIRECTIONS: dict[str, Direction] = {}


def register(name: str, fn: Metric, direction: Direction) -> None:
    """Register a metric under `name` with the way it improves; a duplicate name is a bug.

    `direction` is required rather than defaulted: a default would silently give every new metric
    one orientation, and a run-to-run comparison would then report half of them backwards.
    """
    if name in _REGISTRY:
        raise ValueError(f"metric {name!r} already registered")
    _REGISTRY[name] = fn
    _DIRECTIONS[name] = direction


def metric(name: str, direction: Direction) -> Callable[[Metric], Metric]:
    """Decorator form of `register` — the idiom later phases use to add a metric."""

    def decorate(fn: Metric) -> Metric:
        register(name, fn, direction)
        return fn

    return decorate


def get_metric(name: str) -> Metric:
    """Resolve a registered metric, or raise with the known names (G4)."""
    fn = _REGISTRY.get(name)
    if fn is None:
        raise ValueError(f"unknown metric {name!r}; known: {sorted(_REGISTRY)}")
    return fn


def direction_of(name: str) -> Direction:
    """Resolve which way `name` improves, or raise with the known names (G4).

    Raising beats returning a default: a caller asking about an unregistered metric is comparing
    against something this build cannot score, and answering it with a guess would turn a missing
    metric into a confidently mis-signed verdict.
    """
    direction = _DIRECTIONS.get(name)
    if direction is None:
        raise ValueError(f"unknown metric {name!r}; known: {sorted(_DIRECTIONS)}")
    return direction


def registered_names() -> list[str]:
    """The names of all registered metrics, sorted (for reports and the gate check)."""
    return sorted(_REGISTRY)
