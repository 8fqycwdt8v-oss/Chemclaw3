"""Metric interface + registry — the evaluation layer's core (plan step 2b.1).

Why this layer exists: the Checkmates gate *code* quality, but scientific *output*
quality needs its own measurable gate (docs/research-review.md F7-F9). A metric is a
**pure function** from an evaluation case to a `MetricResult` — value plus provenance
and an optional pass/fail against a config threshold (never a hardcoded one, G3).

The registry is the extension seam for plan step 2b.5: every later capability phase
registers >=1 scientific metric with `@metric(name)`, and a regression in a registered
metric is treated like a failing test. Registration happens on import, so
`evals/__init__.py` imports the seed-metric module to populate the registry.
"""

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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


class MetricError(ValueError):
    """A metric could not be computed for a case (missing/invalid inputs, G4)."""


# A metric is a pure function: it reads a case and returns its scored result.
Metric = Callable[[EvalCase], MetricResult]

_REGISTRY: dict[str, Metric] = {}


def register(name: str, fn: Metric) -> None:
    """Register a metric under `name`; a duplicate name is a programming error."""
    if name in _REGISTRY:
        raise ValueError(f"metric {name!r} already registered")
    _REGISTRY[name] = fn


def metric(name: str) -> Callable[[Metric], Metric]:
    """Decorator form of `register` — the idiom later phases use to add a metric."""

    def decorate(fn: Metric) -> Metric:
        register(name, fn)
        return fn

    return decorate


def get_metric(name: str) -> Metric:
    """Resolve a registered metric, or raise with the known names (G4)."""
    fn = _REGISTRY.get(name)
    if fn is None:
        raise ValueError(f"unknown metric {name!r}; known: {sorted(_REGISTRY)}")
    return fn


def registered_names() -> list[str]:
    """The names of all registered metrics, sorted (for reports and the gate check)."""
    return sorted(_REGISTRY)
