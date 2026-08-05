"""The soak's verdict must refuse the number it cannot resolve.

Every test here is about one failure mode: reporting a trend that the data does not support. The
soak's previous attempt reported `api RSS 643,304 → 650,756 KB` and let the reader infer a leak
from two endpoints — and the series it drew that from is used below, as the case that must come
back *unresolved*. A test suite that only checked the arithmetic of a slope would have passed on
that record too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from chemclaw.cli.soak_report import _series, describe, fit, read_rounds, report

# The five rounds the scratch soak actually recorded, before the container was reclaimed.
_SCRATCH_RSS = [643304.0, 644796.0, 647684.0, 648144.0, 650756.0]


def test_a_flat_series_has_a_slope_of_zero() -> None:
    trend = fit([100.0] * 8)
    assert trend.slope == pytest.approx(0.0)
    assert not trend.resolved


def test_a_clean_ramp_is_resolved_and_its_slope_is_the_ramp() -> None:
    trend = fit([10.0 * i for i in range(8)])
    assert trend.slope == pytest.approx(10.0)
    assert trend.resolved


def test_the_slope_of_a_noisy_flat_series_stays_inside_its_own_error() -> None:
    """Noise must not become a trend: alternating samples fit a slope near zero with a wide band."""
    noisy = [100.0, 130.0, 95.0, 135.0, 98.0, 132.0, 101.0, 129.0]
    trend = fit(noisy)
    assert not trend.resolved
    assert "flat within its own noise" in describe(noisy, "KB")


def test_three_points_are_refused_rather_than_fitted() -> None:
    """A fit with one degree of freedom is dominated by whichever sample was unlucky."""
    trend = fit([1.0, 2.0, 3.0])
    assert not trend.resolved
    assert "too few to fit" in describe([1.0, 2.0, 3.0], "KB")


def test_the_scratch_soaks_rss_series_resolves_a_slope_but_cannot_say_it_continues() -> None:
    """What five rounds of the scratch soak do and do not support — measured, not assumed.

    The plan called these five points "a lead, not a leak" on the grounds that five is few. That
    was wrong, and the fit says so: the residuals are small enough that +1,825 KB/round clears four
    times its own standard error. What n=5 cannot do is fit the *tail*, which is the only thing
    that separates a warm-up from a leak — so the honest verdict names the slope and says the tail
    is too short, rather than either crying leak or calling it flat.
    """
    trend = fit(_SCRATCH_RSS)
    assert trend.resolved
    assert trend.slope == pytest.approx(1825.2, abs=0.5)
    said = describe(_SCRATCH_RSS, "KB")
    assert "grows +1825.2 KB/round" in said
    assert "too few to say whether it settles" in said


def test_a_warm_up_curve_is_separated_from_a_leak() -> None:
    """A series that rises and settles must not get the same words as one that keeps growing."""
    warm_up = [100.0, 140.0, 168.0, 186.0, 196.0, 200.0, 201.0, 200.0, 201.0, 200.0]
    assert "rises then settles" in describe(warm_up, "KB")


def test_a_real_leak_is_named_by_its_two_halves_rather_than_by_the_whole() -> None:
    """Both halves resolved: the verdict compares them to each other, never the tail to the whole.

    The whole *contains* the tail, so on a series that rises in steps the whole-run slope is dragged
    down by an early flat stretch and a tail below it reads as deceleration when nothing
    decelerated. This repository's own soak did exactly that: `whole +2,317 / tail +1,345` was
    reported as "decelerating" at 104 rounds, and at 138 the two halves were +3,166 and +3,177 —
    identical — with the last quarter steeper still.
    """
    leak = [100.0 + 20.0 * i for i in range(12)]
    said = describe(leak, "KB")
    assert "grows and steady" in said
    assert "first half +20.0" in said and "second half +20.0" in said


def test_a_leak_that_gets_worse_is_not_reported_as_steady() -> None:
    """The case tail-versus-whole would have hidden behind a depressed whole-run slope."""
    accelerating = [100.0 + i * i for i in range(16)]
    assert "steepening" in describe(accelerating, "KB")


def test_a_leak_that_is_genuinely_slowing_says_so() -> None:
    """And the opposite must still be reachable, or "steepening" would just be the default."""
    slowing = [100.0 + 400.0 * (i**0.5) for i in range(16)]
    assert "slowing" in describe(slowing, "KB")


def test_a_falling_series_is_reported_as_falling() -> None:
    """Disk free is the series this matters for — a soak that fills the disk must be visible."""
    assert "falls" in describe([100.0 - 3.0 * i for i in range(12)], "GB")


def test_the_record_reader_skips_the_terminal_stop_line() -> None:
    lines = [
        json.dumps({"round": 1, "api_rss_kb": 10}),
        json.dumps({"round": 2, "api_rss_kb": 12}),
        json.dumps({"stop": "disk 3GB below floor 4GB", "round": None}),
    ]
    path = Path("soak.jsonl")
    rounds = read_rounds(_written(path, lines))
    assert [row["round"] for row in rounds] == [1, 2]


def test_a_round_missing_a_sample_costs_that_field_and_not_the_round() -> None:
    """Every scrape in the shell is best-effort; a timed-out curl must not drop the whole round."""
    rounds: list[dict[str, Any]] = [
        {"round": 1, "api_rss_kb": 10, "pool": {"size": 2.0}},
        {"round": 2, "api_rss_kb": None, "pool": None},
        {"round": 3, "api_rss_kb": 14, "pool": {"size": 3.0}},
    ]
    assert _series(rounds, "api_rss_kb") == [10.0, 14.0]
    assert _series(rounds, "pool", "size") == [2.0, 3.0]


def test_the_report_names_every_table_the_record_carries() -> None:
    rounds = [
        {"round": i, "rc": 0, "rows": {"audit_events": 100 * i, "session_messages": 3 * i}}
        for i in range(1, 6)
    ]
    text = report(rounds)
    assert "rows audit_events" in text
    assert "rows session_messages" in text
    assert "rounds 1–5" in text


def test_the_report_names_the_rounds_that_failed() -> None:
    rounds = [{"round": 1, "rc": 0}, {"round": 2, "rc": 1}, {"round": 3, "rc": 0}]
    assert "non-zero exit: [2]" in report(rounds)


def test_an_empty_record_says_so_rather_than_fitting_nothing() -> None:
    assert report([]) == "no rounds recorded"


def _written(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the two tests that touch disk inside pytest's own directory."""
    monkeypatch.chdir(tmp_path)
