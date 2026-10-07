"""Every clock-derived payload jitter must outlast the longest run that will use it.

Live-harness modules vary one input per process so a rerun is not answered from the calculation
cache; a repeated payload rejoins the earlier run and the lane reports work it never did.

The walk finds every assignment (plain or annotated) under `src/chemclaw` whose value calls
`time.time()`, `time.time_ns()` or `time.monotonic()` and contains `%`, and evaluates it over a
24-hour window (above a resumed soak's span). The union across harnesses must also be distinct,
so base temperatures stay at least 1 K apart.

Not seen, by construction: jitter never assigned (inline or returned), derivations without `%`,
and clocks read through an alias such as `from time import time`.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from types import CodeType, SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src" / "chemclaw"

# The soak's default (200 rounds) at its measured round time (~58 s) is ~3.2 h; it resumes across
# container reclaims, so the wall-clock a single record covers is not bounded by one process.
_LONGEST_SOAK_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class _Jitter:
    """One clock-derived expression: where it is written and how to evaluate it."""

    path: str
    lineno: int
    source: str
    code: CodeType


# The clock calls a jitter can be derived from. `time()` is what all three use; `time_ns()` and
# `monotonic()` are here because they are the two forms a fourth copy would most plausibly reach
# for, and a policy that only sees the spelling already in the tree only ever ratifies it.
_CLOCKS = frozenset({"time", "time_ns", "monotonic"})


def _uses_the_clock(node: ast.AST) -> bool:
    """True if the subtree calls one of `time`'s clocks."""
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in _CLOCKS
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "time"
        for n in ast.walk(node)
    )


def _collect() -> list[_Jitter]:
    """Every `... % N ...` assignment under `src/chemclaw` whose left side reads the wall clock.

    Both `x = …` and `x: float = …` forms are matched.
    """
    found: list[_Jitter] = []
    for f in sorted(_SRC_ROOT.rglob("*.py")):
        source = f.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(f))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign | ast.AnnAssign):
                continue
            if node.value is None or not _uses_the_clock(node.value):
                continue
            if not any(
                isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mod) for n in ast.walk(node.value)
            ):
                continue
            expression = ast.Expression(body=node.value)
            ast.fix_missing_locations(expression)
            found.append(
                _Jitter(
                    path=f.relative_to(_REPO_ROOT).as_posix(),
                    lineno=node.lineno,
                    source=ast.get_source_segment(source, node.value) or "",
                    code=compile(expression, filename=str(f), mode="eval"),
                )
            )
    return found


_JITTERS = _collect()


def _values(jitter: _Jitter, stamps: range) -> set[float]:
    """Evaluate one jitter expression at each clock value, with `time.time` stubbed to it."""
    return {
        eval(
            jitter.code,
            {"time": SimpleNamespace(time=lambda s=stamp: float(s))},
        )
        for stamp in stamps
    }


def test_the_walk_finds_every_clock_derived_payload_jitter() -> None:
    """A source walk that matches nothing passes every assertion below.

    Pinned to the exact three files rather than to a count, because the failure this guards is a
    *fourth* copy appearing — and a bare count would be satisfied by any three matches at all.
    """
    assert {j.path for j in _JITTERS} == {
        "src/chemclaw/cli/live_jobs.py",
        "src/chemclaw/cli/live_storm.py",
        "src/chemclaw/cli/storm_behaviours.py",
    }, f"clock-derived jitters found: {sorted((j.path, j.lineno) for j in _JITTERS)}"


def test_no_payload_jitter_repeats_within_the_longest_soak() -> None:
    """Evaluated, not read: one distinct value per second across a 24-hour window."""
    window = range(_LONGEST_SOAK_SECONDS)
    for jitter in _JITTERS:
        distinct = len(_values(jitter, window))
        assert distinct == len(window), (
            f"{jitter.path}:{jitter.lineno} `{jitter.source}` yields only {distinct} distinct "
            f"values across {len(window)}s, so a payload recurs after ~{distinct}s — a rerun "
            "inside that window rejoins the cached run (D-011) and the lane passes on residue"
        )


def test_no_two_harnesses_can_derive_the_same_payload_value() -> None:
    """No two harnesses can derive the same payload value.

    Identical jitter over otherwise identical payloads hashes to the same workflow id and rejoins a
    completed run. Asserted over the whole window, since two grids can be disjoint at one instant
    and overlap later.
    """
    window = range(_LONGEST_SOAK_SECONDS)
    reached = {jitter: _values(jitter, window) for jitter in _JITTERS}
    for one, other in combinations(_JITTERS, 2):
        shared = reached[one] & reached[other]
        assert not shared, (
            f"{one.path}:{one.lineno} and {other.path}:{other.lineno} can derive the same "
            f"{len(shared)} value(s) (e.g. {sorted(shared)[:3]}), so two independent harnesses "
            "hash to one workflow id and the second reads D-011's cache as a failure"
        )


def test_each_jitter_is_constant_within_one_process() -> None:
    """Each jitter is a module constant, so a relaunch within one process derives the same workflow
    id.
    """
    for jitter in _JITTERS:
        assert len(_values(jitter, range(1_700_000_000, 1_700_000_001))) == 1
