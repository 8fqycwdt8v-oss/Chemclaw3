"""Read a soak record and say what the series did — as a fit, never as two endpoints.

The question is whether anything grows that should not. Each series gets a least-squares slope with
its standard error, and a slope inside its own error is reported as unresolved rather than as a
small number.

Warm-up and leak both fit a rising line; they are separated by fitting the two halves separately:
a process that settles has an unresolved slope over its tail, a leak does not.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chemclaw.core.markdown import render_table

# Below four points an ordinary-least-squares standard error is not worth reporting: with n=3 the
# fit has one degree of freedom and the error term is dominated by whichever sample was unlucky.
_MIN_POINTS_TO_FIT = 4

# A slope must clear twice its standard error (~95% for a t-statistic) to count as growth. The one
# unmeasured constant here, applied to every series rather than tuned per series.
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

    Returning `stderr` beside `slope` keeps a caller from reporting the slope alone.
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
    head, tail = fit(values[: len(values) // 2]), fit(values[len(values) // 2 :])
    # A tail too short to fit is named as short, not read as flat: "we did not look" is not "it
    # settled".
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
    # Both halves resolved: compare the two halves, never the tail against the whole — the whole
    # contains the tail, so a stepwise series reads as decelerating when it is not.
    trend = (
        "steady"
        if abs(tail.slope - head.slope) <= _RESOLVING_SIGMA * (head.stderr + tail.stderr)
        else ("slowing" if abs(tail.slope) < abs(head.slope) else "steepening")
    )
    return (
        f"{direction} and {trend} — first half {head.slope:+.1f}, second half {tail.slope:+.1f} "
        f"{unit}/round (± {_RESOLVING_SIGMA * tail.stderr:.1f})"
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
    ]
    tables = sorted({key for row in rounds for key in row.get("rows", {})})
    gauges = sorted({key for row in rounds for key in (row.get("gauges") or {})})
    watched: list[tuple[str, tuple[str, ...], str]] = [
        ("api RSS", ("api_rss_kb",), "KB"),
        ("round seconds", ("secs",), "s"),
        ("disk free", ("disk_gb",), "GB"),
        *[(name, ("gauges", name), "") for name in gauges],
        *[(f"rows {name}", ("rows", name), "rows") for name in tables],
    ]
    rows = [
        [label, f"{values[0]:.0f}", f"{values[-1]:.0f}", describe(values, unit)]
        for label, path, unit in watched
        if (values := _series(rounds, *path))
    ]
    lines.append(render_table(["series", "first", "last", "verdict"], rows, align="lrrl"))
    failed = [row["round"] for row in rounds if row.get("rc") != 0]
    lines += ["", f"rounds with a non-zero exit: {failed or 'none'}"]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Print the soak's fits. `infra/live/soak.sh report` is the intended caller."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path, help="the soak JSONL written by infra/live/soak.sh")
    args = parser.parse_args(argv)
    if not args.record.is_file():
        # `is_file()`, not `exists()`, so a directory argument gets this message instead of a raw
        # `IsADirectoryError`.
        print(f"no soak record at {args.record}")
        return 1
    try:
        rounds = read_rounds(args.record)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        # `infra/live/soak.sh` writes the record line by line, so a truncated file from a killed run
        # is expected input: name the file in one line rather than raising a traceback.
        print(f"cannot read the soak record at {args.record}: {exc}")
        return 1
    print(report(rounds))
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
