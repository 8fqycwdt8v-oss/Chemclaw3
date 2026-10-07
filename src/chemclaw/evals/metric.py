"""Metric interface + registry — the evaluation layer's core.

(Singular `metric` holds the interface and `@metric` registry; the concrete metrics live in the
plural `chemclaw.evals.metrics`.)

Scientific output quality needs its own measurable gate. A metric is a pure function from an
evaluation case to a `MetricResult` — value, provenance and an optional pass/fail against a config
threshold, never a hardcoded one. Capabilities register metrics with `@metric(name, direction)`;
registration happens on import, so `evals/__init__.py` imports the seed-metric module.
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
    # Whether this case's gated metrics are *supposed* to pass. Some cases exist to demonstrate a
    # gate firing, and this lets `make eval --strict` tell those from regressions. Defaults to True,
    # so a new case is gated unless someone says otherwise.
    expect_pass: bool = True


class MetricError(ChemclawError):
    """A metric could not be computed for a case (missing/invalid inputs)."""


# A metric is a pure function: it reads a case and returns its scored result.
Metric = Callable[[EvalCase], MetricResult]

_REGISTRY: dict[str, Metric] = {}
_DIRECTIONS: dict[str, Direction] = {}
_LIVE: set[str] = set()
#: Metrics that return a verdict (`passed`) rather than only a number. See `register`'s `gated`.
_GATED: set[str] = set()


def register(
    name: str, fn: Metric, direction: Direction, *, live: bool = False, gated: bool = False
) -> None:
    """Register a metric under `name` with the way it improves; a duplicate name is a bug.

    `direction` is required: a default would mis-sign half of all run-to-run comparisons.

    `live` says whether scoring executes product code. Most metrics are arithmetic over literals a
    case file commits, which no release can move; `evals.baseline.render_comparison` reports live
    and pinned rows separately. Defaults to False, so live is a deliberate claim.

    `gated` says whether the metric compares against a config threshold and returns a verdict.
    Declared here so the demonstration check can read it from the registry rather than from a run's
    results; `tests/test_evals.py::test_every_scored_metrics_gatedness_is_the_one_it_declares`
    checks it against actual verdicts.
    """
    if name in _REGISTRY:
        raise ValueError(f"metric {name!r} already registered")
    _REGISTRY[name] = fn
    _DIRECTIONS[name] = direction
    if live:
        _LIVE.add(name)
    if gated:
        _GATED.add(name)


def metric(
    name: str, direction: Direction, *, live: bool = False, gated: bool = False
) -> Callable[[Metric], Metric]:
    """Decorator form of `register` — the idiom later phases use to add a metric."""

    def decorate(fn: Metric) -> Metric:
        register(name, fn, direction, live=live, gated=gated)
        return fn

    return decorate


def is_live(name: str) -> bool:
    """Whether scoring `name` runs product code rather than reading a case file's literals.

    An unregistered name answers False rather than raising: the caller reports on a committed
    baseline that may outlive a metric.
    """
    return name in _LIVE


def get_metric(name: str) -> Metric:
    """Resolve a registered metric, or raise with the known names."""
    fn = _REGISTRY.get(name)
    if fn is None:
        raise ValueError(f"unknown metric {name!r}; known: {sorted(_REGISTRY)}")
    return fn


def direction_of(name: str) -> Direction:
    """Resolve which way `name` improves, or raise with the known names.

    Raising beats a default: guessing would turn a missing metric into a confidently mis-signed
    verdict.
    """
    direction = _DIRECTIONS.get(name)
    if direction is None:
        raise ValueError(f"unknown metric {name!r}; known: {sorted(_DIRECTIONS)}")
    return direction


def gated_names() -> set[str]:
    """Every metric that returns a verdict rather than only a number.

    The set a demonstration case is owed against, read from the registry so a metric no case scores
    is still seen.
    """
    return set(_GATED)


def registered_names() -> list[str]:
    """The names of all registered metrics, sorted — the registry's one public read surface.

    Used by tests that assert a metric is registered, so they need not reach into the private dict.
    """
    return sorted(_REGISTRY)
