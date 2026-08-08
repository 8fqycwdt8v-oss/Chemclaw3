"""Every metric name a call site uses is declared — checked statically, because nothing else can.

`core/metrics.py` is deliberately strict: `increment`, `observe` and `bind_gauge` raise `KeyError`
on an undeclared name, and `increment` raises again if the label set does not match the
declaration. `core/metrics_bridge.py` then wraps that in the repository's **only** `except: pass`,
on the correct argument that a metric typo must not fail the operation being counted.

The two together mean a mistyped counter name is invisible at *every* level, DEBUG included: the
`KeyError` is swallowed, no log line is emitted, and the metric simply never appears. The bridge's
docstring is right about what it protects; what nothing protected was the precondition that makes
the swallow harmless — that no call site names an undeclared metric. That held when measured, and
held only by luck.

So this file converts the one invisible swallow into a build-time failure. It reads the *source*
rather than importing and calling, because the defect it guards against is a name that is never
executed on the path a test happens to take.

**Two directions, and the second is not decoration.** Forward: a literal at a call site must be
declared in the matching registry. Backward: a declared metric must appear as a literal somewhere
in `src/` — which is what covers the one call site whose name is a variable
(`api/runner.py` loops over a tuple of the four priced token counters). Typo one of those and the
forward check sees nothing, while the backward check sees the real name lose its last mention.
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

    Matched on the attribute name alone, which is deliberately loose: the receiver is `METRICS` at
    some sites and the lambda parameter of `record_metric` at others (`m`, `metrics`), and pinning
    the receiver would have quietly stopped covering whichever form a new call site chose.
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
    """Declared metric names appearing as a string literal under `src/`, outside `core/metrics.py`.

    Restricted to names that are *already declared* on purpose. `chemclaw_` is also the prefix of
    twelve `ContextVar` names (`chemclaw_current_actor`, `chemclaw_dry_run`, …), so "every
    `chemclaw_*` literal is a metric" is simply false, and a test that assumed it would fail on
    correct code.
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
    """The backward direction: a declaration nothing names emits nothing, and hides a typo.

    This is what covers the one call site whose name is not a literal — `api/runner.py` increments
    the four priced token counters from a loop variable. Misspelling one of those tuple entries is
    invisible to the forward check above and shows up here as the correct name losing its last
    mention in the tree.
    """
    literals = _metric_name_literals()
    unnamed = sorted((set(_COUNTERS) | set(_GAUGES) | set(_HISTOGRAMS)) - set(literals))
    assert not unnamed, (
        "declared metric(s) that no source file names, so nothing can ever emit them — either "
        f"wire them up or delete the declaration: {unnamed}"
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


def test_the_walk_actually_found_the_call_sites() -> None:
    """A source walk that silently matches nothing passes every assertion above.

    Pinned as a floor rather than an exact count so ordinary growth does not fail the build; the
    number it guards against is zero, which is what a renamed method or a moved package would give.
    """
    assert len(_CALLS) >= 30, f"only {len(_CALLS)} metric call sites found — the walk is broken"
    assert {c.method for c in _CALLS} == set(_REGISTRIES), "a whole metric method went unmatched"
