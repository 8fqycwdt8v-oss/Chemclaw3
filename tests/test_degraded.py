"""Warn-and-degrade sites leave a number behind, and the subsystem label set stays enumerable.

Measured on `391b6ec^`, under a stated definition — one `ast.ExceptHandler` whose subtree calls
`.warning()`/`.warn()` and contains no `raise`: **41 such handlers across 34 modules, and exactly 4
of them counted anything** (`api/routes/turns.py:173`, `api/state.py:237`, `durable/publish.py:151`,
`kg/graph.py:155`). Every swallow is individually right — the alternative is failing a
chemist's turn because a preference did not persist — which is exactly why each has to leave a count
behind: from outside, a preference store that has stopped writing, a cost ledger losing every row,
and a redaction filter that never resolved its connector token names all look identical to a healthy
service.

`core/metrics_bridge.degraded` is `agent/audit.py`'s pattern with one owner: count it through the
swallowing bridge, then log it under a stable `degraded[<subsystem>]` marker with the caller's own
logger. These tests drive the real helper against the real registry rather than asserting a call
was made, which is the same discipline `test_metrics_bridge.py` records for the counters it fixed.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from chemclaw.core.metrics import _COUNTER_LABELS, _COUNTERS, METRICS
from chemclaw.core.metrics_bridge import degraded

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src" / "chemclaw"
_COUNTER = "chemclaw_degraded_total"

# Every subsystem name a `degraded()` call site passes. Pinned as a set rather than a count so the
# failure message names what changed, and pinned at all because this is the metric's label value
# space: a label whose values are not enumerable is how a registry ends up with unbounded series.
_EXPECTED_SUBSYSTEMS = {
    "cost_ledger",
    "job_resume",
    "log_redaction",
    "plan_approval",
    "preferences",
    "skill_manifest",
    "tool_result_store",
    "transcript_projection",
}


def _subsystem_argument(node: ast.Call) -> ast.expr | None:
    """The `subsystem` argument of a `degraded(...)` call, in either form it can be written.

    Keyword before positional, because a call may pass `logger` positionally and `subsystem=` by
    name. Returns None only for a call that passes no subsystem at all, which `mypy` rejects.
    """
    keyword = next((kw.value for kw in node.keywords if kw.arg == "subsystem"), None)
    if keyword is not None:
        return keyword
    return node.args[1] if len(node.args) > 1 else None


def _is_degraded_call(node: ast.AST) -> bool:
    """Whether `node` calls the helper — `degraded(...)` or `<module>.degraded(...)`.

    Both spellings, because matching only the bare name is not a narrowing, it is a hole:
    `metrics_bridge.degraded(logger, f"conn_{n}", "x")` passed this whole file while a computed
    label reached the metric. Matched on the attribute name alone for the same reason
    `test_metric_declarations.py` matches `increment` that way — pinning the receiver would stop
    covering whichever import form a new call site chose.
    """
    if not isinstance(node, ast.Call):
        return False
    return (isinstance(node.func, ast.Name) and node.func.id == "degraded") or (
        isinstance(node.func, ast.Attribute) and node.func.attr == "degraded"
    )


def _call_site_subsystems() -> dict[str, list[str]]:
    """Read the literal subsystem argument of every `degraded(...)` call under `src/chemclaw`."""
    found: dict[str, list[str]] = {}
    for f in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            if not _is_degraded_call(node):
                continue
            assert isinstance(node, ast.Call)
            subsystem = _subsystem_argument(node)
            assert isinstance(subsystem, ast.Constant) and isinstance(subsystem.value, str), (
                f"{f}:{node.lineno}: degraded()'s subsystem must be a literal — it is a metric "
                "label value, and a computed one cannot be bounded by reading the source"
            )
            found.setdefault(subsystem.value, []).append(
                f"{f.relative_to(_REPO_ROOT).as_posix()}:{node.lineno}"
            )
    return found


def test_the_subsystem_label_space_is_exactly_what_is_declared() -> None:
    """Both directions: a new subsystem is a deliberate addition, a removed one loses its row."""
    observed = _call_site_subsystems()
    assert set(observed) == _EXPECTED_SUBSYSTEMS, (
        f"the `subsystem` label value set changed. Sites: {dict(sorted(observed.items()))}"
    )


def test_the_enumeration_sees_both_call_spellings_and_both_argument_forms() -> None:
    """The extractor itself, pinned — because two ways past it were found by measurement.

    `metrics_bridge.degraded(logger, f"conn_{n}", "x")` and
    `degraded(logger, subsystem=f"conn_{n}", message="x")` each passed this whole file while
    putting a computed value on a metric label, which is the one thing `core/metrics.py` says
    cannot happen ("nothing a request carries can reach this label"). A per-connector or per-actor
    f-string would have reached it silently, bounded only by `_MAX_SERIES_PER_COUNTER`.
    """
    calls = [
        node
        for node in ast.walk(
            ast.parse(
                'degraded(logger, "a", "m")\n'
                'metrics_bridge.degraded(logger, "b", "m")\n'
                'degraded(logger, subsystem="c", message="m")\n'
            )
        )
        if _is_degraded_call(node)
    ]
    assert len(calls) == 3, "a call spelling the source walk cannot see"
    seen = [_subsystem_argument(call) for call in calls if isinstance(call, ast.Call)]
    assert [s.value for s in seen if isinstance(s, ast.Constant)] == ["a", "b", "c"]


def test_the_counter_is_declared_with_its_label() -> None:
    """The registry refuses an undeclared label, and `record_metric` would swallow that refusal."""
    assert _COUNTER in _COUNTERS
    assert _COUNTER_LABELS[_COUNTER] == ("subsystem",)


def test_a_degradation_is_counted_under_its_own_subsystem(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole point: the swallow leaves a number, attributed to what lost function.

    Read off the registry rather than off a mock, so this fails against a helper that logs and
    forgets — which is precisely the state 32 of those 35 modules were in.
    """
    logger = logging.getLogger("chemclaw.test.degraded")
    before = METRICS.value(_COUNTER)
    with caplog.at_level(logging.ERROR, logger=logger.name):
        try:
            raise RuntimeError("the sink is down")
        except RuntimeError:
            degraded(
                logger, "preferences", "could not persist preference %r for %s", "units", "ana"
            )

    assert METRICS.value(_COUNTER) == before + 1
    record = caplog.records[-1]
    assert record.levelno == logging.ERROR
    assert record.name == "chemclaw.test.degraded", "the caller's logger names the failing module"
    assert record.getMessage() == (
        "degraded[preferences]: could not persist preference 'units' for ana"
    )
    assert record.exc_info is not None, "the active exception travels with the record"


def test_the_series_are_separate_per_subsystem() -> None:
    """An alert has to name the failing subsystem, which one undifferentiated total cannot do."""
    logger = logging.getLogger("chemclaw.test.degraded")
    rendered_before = METRICS.render()
    degraded(logger, "cost_ledger", "ledger down", exc_info=False)
    rendered = METRICS.render()
    assert 'chemclaw_degraded_total{subsystem="cost_ledger"}' in rendered
    assert rendered != rendered_before


def _boom(*_args: object, **_kwargs: object) -> None:
    """Stand in for a registry that refuses the update (an undeclared name or label)."""
    raise KeyError("undeclared counter")


def test_a_registry_failure_cannot_replace_the_degradation_it_reports(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reason the helper lives beside `record_metric` rather than in the registry module.

    `Metrics.increment` raises on an undeclared name or label, and `degraded` is called from inside
    `except` blocks — the one place where a raising metric update would substitute a `KeyError`
    from the reporting for the failure being reported. Proved by breaking the registry underneath
    it and requiring the log line to arrive anyway, rather than by reading the swallow.
    """
    logger = logging.getLogger("chemclaw.test.degraded")
    original = METRICS.increment
    try:
        METRICS.increment = _boom  # type: ignore[method-assign]
        with caplog.at_level(logging.ERROR, logger=logger.name):
            degraded(logger, "preferences", "the store is unreachable", exc_info=False)
    finally:
        METRICS.increment = original  # type: ignore[method-assign]

    assert caplog.records[-1].getMessage() == "degraded[preferences]: the store is unreachable"
