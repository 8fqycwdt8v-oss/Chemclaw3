"""Every clock-derived payload jitter must outlast the longest run that will use it.

Three live-harness modules vary one physical input per process so a rerun cannot be answered from
the calculation cache. That is not decoration: a durable job's workflow id is a hash of its
payload, a duplicate launch deliberately rejoins the existing run rather than recomputing (D-011),
so a payload that repeats makes the lane report a working durable path it never exercised.

The mechanism is right and the *modulus* is the whole guarantee — and it was wrong in two of the
three copies:

- `cli/storm_behaviours.py` carries the reasoned value, and a nine-line comment recording how it
  was learned: `% 719` recurs every ~12 minutes, and 6 of 81 soak rounds failed that family with
  "0 job_records row(s) written". Nothing was broken; D-011's cache had answered the payload from
  an earlier round and the harness read that as a failure.
- `cli/live_storm.py` had `% 971` — 16.2 minutes, 1.35x the period already measured failing.
- `cli/live_jobs.py` had `% 25`: **25 distinct temperatures that ever exist**. After ~25 runs
  against one database the cache holds every one of them and the lane is green forever, computing
  nothing.

So the fix is a value, and a value copied into three places is a value that will be wrong in one of
them again. This file finds the expressions rather than being told where they are — any
`… time.time() … % N …`, anywhere under `src/chemclaw` — and *evaluates* each one across a window
of clock values instead of reading its modulus, because the property is "no payload repeats within
a run", not "the literal 100000 appears".

**Per-expression distinctness is not the property; the union is.** Fixing `live_jobs` by copying
`storm_behaviours`'s expression gave the two modules the identical jitter *and* the identical base
temperature over otherwise byte-identical payloads, so at `t = 1700000123` both derived 298.15123
and the payloads compared equal — a `make live-jobs` during a soak round hashed to the storm's
workflow id and read D-011's cache as "0 job_records row(s) written". Each grid spans base + [0, 1)
K, so the bases (298.15, 300.0, 301.15) must stay ≥ 1 K apart, and that is asserted rather than
trusted.

**What this file can and cannot see.** It matches an assignment — plain or annotated — whose value
subtree calls `time.time()`, `time.time_ns()` or `time.monotonic()` and contains a `%`. Deliberate
limits, stated because the previous version of this docstring claimed the walk pinned "all three
periods so a fourth copy cannot regress it" while an *annotated* assignment slipped past it
silently:

- a jitter that is never assigned — computed inline in a payload literal, or `return`ed from a
  helper — has no `Assign`/`AnnAssign` node to match;
- a derivation without `%` (`random`, a counter, `time() // N`) is out of scope by construction,
  since the property being checked is a modulus's period;
- a clock read through an alias (`from time import time`) is not seen, because the matcher pins
  the `time.<clock>()` attribute form.

Each of these is a hole a future fourth copy could walk through. They are named rather than closed
because the check is a net for a copy-paste, not a proof, and a net that claims to be a proof is
the thing this whole lane is about.

**Where the window comes from.** `infra/live/soak.sh` runs 200 rounds by default and a round was
measured at ~58 s, so a default soak spans ~3.2 hours; it is checkpointed and resumes after a
container reclaim, so a record can span considerably more wall-clock than one process does. 24
hours is the round number above that with room to spare, and all three copies clear it (100,000
values on a one-second grid ≈ 27.8 h).
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
    """Every `... % N ...` expression under `src/chemclaw` whose left side reads the wall clock.

    Both assignment forms: `x = …` and `x: float = …`. The annotated one was invisible until an
    adversarial review measured it — a probe module carrying `_T: float = 298.15 + (int(time.time())
    % 7)` passed the whole file, while the identical line without the annotation failed two of its
    tests. What is still invisible is stated in the module docstring rather than fixed here.
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
        eval(  # noqa: S307 - the expression comes from this repository's own source, not input
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
    """Evaluated, not read: one distinct value per second across a 24-hour window.

    Measured on the unfixed tree this replaces, the same window gave 25 distinct values for
    `live_jobs` and 971 for `live_storm` — so a soak longer than 25 seconds, respectively 16
    minutes, was re-launching payloads the cache had already answered.
    """
    window = range(_LONGEST_SOAK_SECONDS)
    for jitter in _JITTERS:
        distinct = len(_values(jitter, window))
        assert distinct == len(window), (
            f"{jitter.path}:{jitter.lineno} `{jitter.source}` yields only {distinct} distinct "
            f"values across {len(window)}s, so a payload recurs after ~{distinct}s — a rerun "
            "inside that window rejoins the cached run (D-011) and the lane passes on residue"
        )


def test_no_two_harnesses_can_derive_the_same_payload_value() -> None:
    """The union, which per-expression distinctness does not imply and did not hold.

    Fixing `live_jobs`'s `% 25` by copying `storm_behaviours`'s expression gave the two modules the
    *identical* jitter and otherwise byte-identical payloads. Measured at `t = 1700000123`:

        live_jobs temp        298.15123
        storm_behaviours temp 298.15123
        payloads equal: True

    Before that fix the two grids intersected in exactly one value; after it they were the same
    set. A `make live-jobs` launched in the same second as a soak round then hashes to the storm's
    workflow id, rejoins its completed run and writes no `job_records` row — the "0 job_records
    row(s) written" false failure the nine-line comment exists to prevent, reached *between*
    harnesses rather than within one.

    Asserted over the same 24-hour window as the test above, because two grids can be disjoint at
    one instant and overlap an hour later.
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
    """The value is read once at import, so a relaunch inside a run derives the same workflow id.

    The counterpart to the test above and the reason this is a *module constant* rather than a
    function: `live_jobs` relaunches its own job to assert idempotency, which only means anything
    if the second launch computes the same id.
    """
    for jitter in _JITTERS:
        assert len(_values(jitter, range(1_700_000_000, 1_700_000_001))) == 1
