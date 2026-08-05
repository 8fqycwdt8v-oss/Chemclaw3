"""Read a soak record and say what the series did — as a fit, never as two endpoints.

The soak exists to answer one question no single run can: does anything grow that should not?
The previous attempt answered it the way that is always available and never sound — it subtracted
the first sample from the last. api RSS went 643,304 → 650,756 KB across five rounds, which reads
as a 7 MB leak and is equally consistent with a warm-up curve, with ordinary allocator jitter, and
with a leak ten times larger hiding under a noisy fifth sample.

So the verdict here is a *slope with its standard error*, and a series whose slope is inside its own
error is reported as unresolved rather than as a small number. That is the same rule `live_storm`'s
knee finder learned the hard way in
`D-2026-08-04-a-plateau-needs-the-noise-you-measured-it-with`: a threshold chosen before the noise
is measured produces a confident answer at random.

The second half exists to separate the two shapes that both fit a rising line. A process that
warms up and settles has a resolved slope over the whole series and an unresolved one over its
tail; a leak has both, with the same sign. Nothing else distinguishes them at these lengths, and
"the tail is flat" is the claim a memory leak cannot make.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Below four points an ordinary-least-squares standard error is not worth reporting: with n=3 the
# fit has one degree of freedom and the error term is dominated by whichever sample was unlucky.
_MIN_POINTS_TO_FIT = 4

# A slope must clear twice its own standard error before it is called growth. Two is the
# conventional ~95% line for a t-statistic at these sample sizes and, more to the point, it is
# chosen here because it is the *only* number in this module that is not measured — so it is
# named once, explained, and applied to every series rather than tuned per series.
_RESOLVING_SIGMA = 2.0


@dataclass(frozen=True)
class Trend:
    """An ordinary-least-squares fit of one series against the round index."""

    slope: float
    """Units per round."""

    stderr: float
    """Standard error of `slope`. Infinite when the fit has no residual degrees of freedom."""

    n: int
    """How many points the fit saw."""

    @property
    def resolved(self) -> bool:
        """Whether the slope is distinguishable from flat at this length and this noise."""
        return self.n >= _MIN_POINTS_TO_FIT and abs(self.slope) > _RESOLVING_SIGMA * self.stderr


def fit(values: Sequence[float]) -> Trend:
    """Least-squares slope of `values` against their index, with the slope's standard error.

    Written out rather than pulled from numpy because the whole point is that the error term is
    visible: a caller that can see `stderr` beside `slope` cannot accidentally report the slope
    alone, which is the failure this module exists to prevent.
    """
    n = len(values)
    if n < 2:
        return Trend(slope=0.0, stderr=float("inf"), n=n)
    xs = [float(i) for i in range(n)]
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0.0:
        return Trend(0.0, float("inf"), n)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values, strict=True)) / sxx
    intercept = mean_y - slope * mean_x
    residuals = [y - (intercept + slope * x) for x, y in zip(xs, values, strict=True)]
    if n <= 2:
        return Trend(slope, float("inf"), n)
    variance = sum(r * r for r in residuals) / (n - 2)
    return Trend(slope, (variance / sxx) ** 0.5, n)


def describe(values: Sequence[float], unit: str) -> str:
    """One sentence about what a series did, refusing to name a number it cannot resolve."""
    whole = fit(values)
    if not whole.resolved:
        band = _RESOLVING_SIGMA * whole.stderr
        if whole.n < _MIN_POINTS_TO_FIT:
            return f"unresolved — {whole.n} point(s) is too few to fit"
        return f"flat within its own noise (slope {whole.slope:+.1f} ± {band:.1f} {unit}/round)"
    direction = "grows" if whole.slope > 0 else "falls"
    tail = fit(values[len(values) // 2 :])
    # A tail that is too short to fit and a tail that is genuinely flat both fail `resolved`, and
    # they are opposite statements — "it settled" versus "we did not look". Collapsing them is how
    # a five-round record gets read as a plateau, so the short case is named as short.
    if tail.n < _MIN_POINTS_TO_FIT:
        return (
            f"{direction} {whole.slope:+.1f} {unit}/round "
            f"(± {_RESOLVING_SIGMA * whole.stderr:.1f}); "
            f"{tail.n} tail point(s) is too few to say whether it settles"
        )
    if not tail.resolved:
        return (
            f"rises then settles — {whole.slope:+.1f} {unit}/round over the whole run, "
            f"flat within its noise over the last {tail.n} rounds"
        )
    return (
        f"{direction} {whole.slope:+.1f} {unit}/round "
        f"(± {_RESOLVING_SIGMA * whole.stderr:.1f}), still {tail.slope:+.1f} over the tail"
    )


def read_rounds(path: Path) -> list[dict[str, Any]]:
    """Parse the soak record, skipping the terminal `stop` line the script writes on exit."""
    rounds: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        # The terminal line carries `"round": null`, so presence of the key is not the test.
        if isinstance(row.get("round"), int):
            rounds.append(row)
    return rounds


def _series(rounds: Sequence[dict[str, Any]], *path: str) -> list[float]:
    """Pull one nested numeric series out of the rounds, dropping rounds that lack it."""
    out: list[float] = []
    for row in rounds:
        cursor: Any = row
        for key in path:
            if not isinstance(cursor, dict) or key not in cursor:
                cursor = None
                break
            cursor = cursor[key]
        if isinstance(cursor, int | float):
            out.append(float(cursor))
    return out


def report(rounds: Sequence[dict[str, Any]]) -> str:
    """The soak's whole deliverable: one line per series, each a fit rather than a difference."""
    if not rounds:
        return "no rounds recorded"
    lines = [
        f"# Soak: {len(rounds)} round(s), rounds {rounds[0]['round']}–{rounds[-1]['round']}",
        "",
        "| series | first | last | verdict |",
        "| --- | ---: | ---: | --- |",
    ]
    tables = sorted({key for row in rounds for key in row.get("rows", {})})
    watched: list[tuple[str, tuple[str, ...], str]] = [
        ("api RSS", ("api_rss_kb",), "KB"),
        ("round seconds", ("secs",), "s"),
        ("pool size", ("pool", "size"), "conns"),
        ("pool waiting", ("pool", "requests_waiting"), "waiters"),
        ("disk free", ("disk_gb",), "GB"),
        *[(f"rows {name}", ("rows", name), "rows") for name in tables],
    ]
    for label, path, unit in watched:
        values = _series(rounds, *path)
        if not values:
            continue
        lines.append(f"| {label} | {values[0]:.0f} | {values[-1]:.0f} | {describe(values, unit)} |")
    failed = [row["round"] for row in rounds if row.get("rc") != 0]
    lines += ["", f"rounds with a non-zero exit: {failed or 'none'}"]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Print the soak's fits. `infra/live/soak.sh report` is the intended caller."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path, help="the soak JSONL written by infra/live/soak.sh")
    args = parser.parse_args(argv)
    if not args.record.exists():
        print(f"no soak record at {args.record}")
        return 1
    print(report(read_rounds(args.record)))
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
