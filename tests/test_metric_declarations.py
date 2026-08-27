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
in `src/` — which is what covers the **two** call sites whose name is a variable: `api/runner.py`
loops over a tuple of the four priced token counters, and `core/metrics_bridge.py` increments
`_DEGRADED_COUNTER`, a module constant. Typo either and the forward check sees nothing, while the
backward check sees the real name lose its last mention.

The second of those was added by the same commit as this file, which is worth saying plainly: the
counter this diff introduced sits in the blind spot of the test this diff introduced. It is not a
live risk — `_DEGRADED_COUNTER`'s value is still a literal in that module, so the backward check
holds it, and `tests/test_degraded.py` drives `degraded()` against the real registry — but "the one
variable call site" was true for about as long as it took to write it down.
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


# The metrics the registry emits about *itself*, and so the only names the scan below cannot see:
# `core/metrics.py` is excluded from it (that is where every name is declared), and these two have
# no call site outside it by construction — one is incremented when a bound gauge source raises
# during `render`, the other when a sample is refused at the per-metric label-set cap. Both are
# held to a producer by `test_the_registry_really_does_emit_the_metrics_it_is_exempted_for`, so
# this is a redirected check rather than a waiver.
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
    """The backward direction: a declaration nothing names emits nothing, and hides a typo.

    This is what covers the two call sites whose name is not a literal — `api/runner.py` increments
    the four priced token counters from a loop variable, and `core/metrics_bridge.py` increments
    the `_DEGRADED_COUNTER` constant. Misspelling either is invisible to the forward check above
    and shows up here as the correct name losing its last mention in the tree.
    """
    literals = _metric_name_literals()
    declared = set(_COUNTERS) | set(_GAUGES) | set(_HISTOGRAMS)
    unnamed = sorted(declared - set(literals) - _SELF_EMITTED)
    assert not unnamed, (
        "declared metric(s) that no source file names, so nothing can ever emit them — either "
        f"wire them up or delete the declaration: {unnamed}"
    )


def test_the_registry_really_does_emit_the_metrics_it_is_exempted_for() -> None:
    """`_SELF_EMITTED` is an exemption from the scan, so it needs its own producer check.

    The scan above skips `core/metrics.py`, because that is where every name is *declared* and
    counting a declaration as a mention would make the backward direction vacuous. That exclusion
    is right for every metric an ordinary call site emits and wrong for the two the registry emits
    about *itself* — a gauge whose source raised, and a sample dropped at the cardinality cap —
    which have no call site anywhere else by construction.

    Exempting them without checking anything would hand the next self-emitted metric a free pass,
    which is the shape of hole this whole file exists to close. So the exemption is paid for here:
    each name must appear as a literal inside `core/metrics.py` at a line that is not part of a
    declaration table, i.e. somewhere the module actually records it.
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
    """`chemclaw_durable_unreachable_total` is incremented from its one declared population.

    **The one thing in this file that checks a meaning**, and it is here because everything else
    here checks a name. The counter is declared "turns whose durable-subsystem health probe failed
    (Temporal did not answer)" and `ChemclawDurableUnreachable` alerts on it with the summary
    "Temporal is not answering its health probe". A later lane added a second increment in
    `api/middleware._subsystem_unavailable`, which fires per **HTTP request** for the whole
    `SubsystemUnavailableError` family — `DocumentIndexError`, a pgvector failure with no Temporal
    in it, included. Nothing failed: the name was declared and took no labels, so both checks above
    were satisfied while the series carried two populations with two different denominators and the
    alert summed them under a sentence true of only one.

    Pinned to the module rather than the line so ordinary edits do not fail the build. The
    request-path population is `chemclaw_subsystem_unavailable_total`, the sibling of
    `chemclaw_db_unavailable_total`; a handler that sheds requests counts the requests it sheds.
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
