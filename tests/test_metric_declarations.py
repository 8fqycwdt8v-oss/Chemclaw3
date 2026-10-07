"""Every metric name a call site uses is declared, checked statically.

`core/metrics.py` raises `KeyError` on an undeclared name, and `core/metrics_bridge.py` swallows
it so a metric typo cannot fail the operation; together a mistyped name is silent. This file
turns that into a build-time failure by reading the source, since the bad name may never run on a
tested path. Forward: a literal at a call site is declared. Backward: a declared metric appears as
a literal in `src/`, which covers call sites using a variable (`api/runner.py`'s token counters,
`metrics_bridge`'s `_DEGRADED_COUNTER`).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from chemclaw.core.metrics import _COUNTER_LABELS, _COUNTERS, _GAUGES, _HISTOGRAMS

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src" / "chemclaw"
# The declaration tables' own home: its literals *are* the declarations, so they are not call sites.
_METRICS_MODULE = _SRC_ROOT / "core" / "metrics.py"

# Method name -> the table that declares what it may be called with. `bind_gauge` rather than
# `set_gauge`: gauges here are bound to a live source, never written, so there is nothing to set.
_REGISTRIES: dict[str, dict[str, str]] = {
    "increment": _COUNTERS,
    "observe": _HISTOGRAMS,
    "bind_gauge": _GAUGES,
}

# What each method's table is called in a failure message, so the message names the right table.
_KINDS: dict[str, str] = {"increment": "counter", "observe": "histogram", "bind_gauge": "gauge"}


@dataclass(frozen=True)
class _Call:
    """One metric call site: where it is, which method, and the name it passed (if a literal)."""

    path: str
    lineno: int
    method: str
    name: str | None
    labels: frozenset[str] | None  # None when the labels argument is absent or not a literal dict


def _label_keys(node: ast.Call) -> frozenset[str] | None:
    """The literal keys of a `labels=` argument (positional or keyword), or None if not literal."""
    labels: ast.expr | None = next(
        (kw.value for kw in node.keywords if kw.arg == "labels"),
        node.args[2] if len(node.args) > 2 else None,
    )
    if not isinstance(labels, ast.Dict):
        return None
    keys = [k for k in labels.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    if len(keys) != len(labels.keys):  # a `**rest` or computed key: nothing static to check
        return None
    return frozenset(str(k.value) for k in keys)


def _collect_calls() -> list[_Call]:
    """Every `<something>.increment/observe/bind_gauge(...)` written under `src/chemclaw`.

    Matched on the attribute name alone because receivers vary (`METRICS`, or a `record_metric`
    lambda parameter).
    """
    calls: list[_Call] = []
    for f in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            method = node.func.attr
            if method not in _REGISTRIES or not node.args:
                continue
            first = node.args[0]
            name = first.value if isinstance(first, ast.Constant) else None
            calls.append(
                _Call(
                    path=f.relative_to(_REPO_ROOT).as_posix(),
                    lineno=node.lineno,
                    method=method,
                    name=name if isinstance(name, str) else None,
                    labels=_label_keys(node) if method == "increment" else None,
                )
            )
    return calls


def _metric_name_literals() -> dict[str, list[str]]:
    """Declared metric names appearing as string literals under `src/`, outside `core/metrics.py`.

    Restricted to declared names, because many `ContextVar` names also start with `chemclaw_`.
    """
    declared = set(_COUNTERS) | set(_GAUGES) | set(_HISTOGRAMS)
    found: dict[str, list[str]] = {}
    for f in sorted(_SRC_ROOT.rglob("*.py")):
        if f == _METRICS_MODULE:
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in declared:
                site = f"{f.relative_to(_REPO_ROOT).as_posix()}:{node.lineno}"
                found.setdefault(str(node.value), []).append(site)
    return found


# Metrics the registry emits about itself, which the scan cannot see because `core/metrics.py` is
# excluded from it: one counts a raising gauge source during `render`, the other a sample refused at
# the label-set cap. `test_the_registry_really_does_emit_the_metrics_it_is_exempted_for` holds them
# to a producer.
_SELF_EMITTED = frozenset(
    {"chemclaw_gauge_read_failures_total", "chemclaw_metric_series_dropped_total"}
)

_CALLS = _collect_calls()


def test_every_metric_name_at_a_call_site_is_declared() -> None:
    """A literal passed to increment/observe/bind_gauge is in that method's declaration table."""
    undeclared = [
        f"  {c.path}:{c.lineno}: {c.method}({c.name!r}) is not a declared {_KINDS[c.method]}"
        for c in _CALLS
        if c.name is not None and c.name not in _REGISTRIES[c.method]
    ]
    assert not undeclared, (
        "metric name(s) no registry declares — `record_metric` swallows the KeyError at every "
        "level, so these would never be emitted and never be logged:\n" + "\n".join(undeclared)
    )


def test_every_declared_metric_is_named_somewhere_in_the_source() -> None:
    """Every declared metric is named somewhere in the source.

    A declaration nothing names emits nothing and hides a typo at a variable call site, which shows
    up here as the correct name losing its last mention.
    """
    literals = _metric_name_literals()
    declared = set(_COUNTERS) | set(_GAUGES) | set(_HISTOGRAMS)
    unnamed = sorted(declared - set(literals) - _SELF_EMITTED)
    assert not unnamed, (
        "declared metric(s) that no source file names, so nothing can ever emit them — either "
        f"wire them up or delete the declaration: {unnamed}"
    )


def test_the_registry_really_does_emit_the_metrics_it_is_exempted_for() -> None:
    """The registry really emits the metrics exempted as `_SELF_EMITTED`.

    Each must appear as a literal inside `core/metrics.py` outside a declaration table, i.e. where
    the module records it, so the exemption cannot become a free pass.
    """
    tree = ast.parse(_METRICS_MODULE.read_text(encoding="utf-8"), filename=str(_METRICS_MODULE))
    # The declaration tables are dict literals mapping a name to help text or to labels. A name
    # emitted by the module appears somewhere that is *not* a dict key, so collect those.
    declared_keys: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            declared_keys.update(id(key) for key in node.keys if key is not None)
    emitted = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in declared_keys
    }
    missing = sorted(_SELF_EMITTED - emitted)
    assert not missing, (
        "metric(s) exempted from the backward scan as self-emitted, but `core/metrics.py` never "
        f"names them outside a declaration table — so nothing emits them at all: {missing}"
    )


def test_every_literal_label_set_matches_its_counter_declaration() -> None:
    """`increment` requires exactly the declared labels, and that KeyError is swallowed too.

    Only literal `labels={...}` dictionaries are checkable; a computed label mapping (the runner's
    `spend_labels`) is skipped rather than guessed at.
    """
    wrong = [
        f"  {c.path}:{c.lineno}: {c.name!r} declares labels "
        f"{sorted(_COUNTER_LABELS.get(c.name or '', ()))}, call passes {sorted(c.labels or ())}"
        for c in _CALLS
        if c.method == "increment"
        and c.name in _COUNTERS
        and c.labels is not None
        and c.labels != frozenset(_COUNTER_LABELS.get(c.name or "", ()))
    ]
    assert not wrong, "label set(s) that do not match the declaration:\n" + "\n".join(wrong)


def test_the_durable_counter_counts_only_the_durable_probe() -> None:
    """`chemclaw_durable_unreachable_total` is incremented only by the durable health probe.

    It is declared as failed Temporal probes and alerted on as such, so an increment from the HTTP
    request path (whose errors include non-Temporal subsystems) would mix two populations. That
    population is `chemclaw_subsystem_unavailable_total`. Pinned to the module, not the line.
    """
    sites = sorted(
        f"{c.path}:{c.lineno}" for c in _CALLS if c.name == "chemclaw_durable_unreachable_total"
    )
    assert [site.rsplit(":", 1)[0] for site in sites] == ["src/chemclaw/api/runner.py"], (
        "the durable counter is incremented outside the per-turn Temporal health probe it "
        f"declares, so its series mixes populations and its alert reads their sum: {sites}"
    )


def test_the_walk_actually_found_the_call_sites() -> None:
    """A source walk that silently matches nothing passes every assertion above.

    Pinned as a floor rather than an exact count so ordinary growth does not fail the build; the
    number it guards against is zero, which is what a renamed method or a moved package would give.
    """
    assert len(_CALLS) >= 30, f"only {len(_CALLS)} metric call sites found — the walk is broken"
    assert {c.method for c in _CALLS} == set(_REGISTRIES), "a whole metric method went unmatched"
