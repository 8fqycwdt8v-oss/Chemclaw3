"""The delegation comparison drops what it cannot measure, loudly, and never averages it in.

Every case here is a way the measurement can be wrong *while looking right*, which is the failure
the instrument this replaces actually had: `data/evals/probes/m12/routing.yaml` measured delegation
rate over one-tool probes and reported two numbers seven-fold apart as though each were a result.

The arithmetic cases are thin on purpose. Medians and subtractions are not where this goes wrong —
the drops are, because every one of them is a task silently becoming part of an average about
something else.
"""

from __future__ import annotations

import pytest

from chemclaw.evals.delegation import (
    BASELINE_ARM,
    MINIMUM_REPEATS,
    ArmRun,
    NoComparableTask,
    aggregate_runs,
    compare_arms,
)

ARM = "helper"


def _runs(
    task_id: str,
    arm: str,
    *,
    quality: float,
    tokens: int,
    seconds: float,
    delegated: bool,
    repeats: int = MINIMUM_REPEATS,
) -> list[ArmRun]:
    """`repeats` identical runs, so a case varies exactly the one thing it is about."""
    return [
        ArmRun(
            task_id=task_id,
            arm=arm,
            quality=quality,
            billed_tokens=tokens,
            wall_clock_seconds=seconds,
            delegated=delegated,
        )
        for _ in range(repeats)
    ]


def _pair(
    task_id: str,
    *,
    base_quality: float = 1.0,
    arm_quality: float = 1.0,
    base_tokens: int = 1000,
    arm_tokens: int = 500,
    base_seconds: float = 10.0,
    arm_seconds: float = 12.0,
    base_delegated: bool = False,
    arm_delegated: bool = True,
    repeats: int = MINIMUM_REPEATS,
) -> list[ArmRun]:
    """A complete, comparable task: both arms, enough repeats, each behaving as its name says."""
    return [
        *_runs(
            task_id,
            BASELINE_ARM,
            quality=base_quality,
            tokens=base_tokens,
            seconds=base_seconds,
            delegated=base_delegated,
            repeats=repeats,
        ),
        *_runs(
            task_id,
            ARM,
            quality=arm_quality,
            tokens=arm_tokens,
            seconds=arm_seconds,
            delegated=arm_delegated,
            repeats=repeats,
        ),
    ]


def test_a_complete_pair_is_compared_on_all_three_axes() -> None:
    """The ordinary case, and the sign convention both cost axes are stated in."""
    report = compare_arms(_pair("sweep"), arm=ARM)

    assert report.compared == 1
    [comparison] = report.comparisons
    assert comparison.task_id == "sweep"
    assert comparison.quality_delta == 0.0
    # Negative means the arm spent *less*: 500 against 1,000.
    assert comparison.token_delta == -500
    # Positive means it took *longer*: 12 s against 10 s. Both signs point the same way — "what the
    # arm did, relative to the baseline" — so nothing has to remember which axis is good when.
    assert comparison.wall_clock_delta == pytest.approx(2.0)
    assert report.median_token_ratio == pytest.approx(0.5)
    assert report.median_wall_clock_ratio == pytest.approx(1.2)


def test_a_baseline_that_delegated_is_contaminated_rather_than_compared() -> None:
    """The defect a behavioural arm actually has, and the reason `delegated` is on every run.

    `no-helper` is the model being *asked* not to call `task`, because the tool cannot be taken
    away — `SubAgentMiddleware` is required and an empty roster makes upstream re-insert its own.
    So compliance is an observation. A task where the baseline delegated anyway compares delegation
    against delegation, and averaging it in would pull every aggregate toward zero effect: the
    answer this measurement most needs to be unable to produce by accident.
    """
    runs = [
        *_pair("clean-a"),
        *_pair("clean-b"),
        *_pair("dirty", base_delegated=True),
    ]

    report = compare_arms(runs, arm=ARM)

    assert report.contaminated == ["dirty"]
    assert [c.task_id for c in report.comparisons] == ["clean-a", "clean-b"]
    assert "dirty" not in report.undelegated, "contamination outranks the arm's own behaviour"


def test_an_arm_that_never_delegated_is_reported_rather_than_credited() -> None:
    """The mirror defect: the `helper` arm measuring the baseline under a different label."""
    runs = [*_pair("real-a"), *_pair("real-b"), *_pair("inert", arm_delegated=False)]

    report = compare_arms(runs, arm=ARM)

    assert report.undelegated == ["inert"]
    # **Reported, and still compared** — this is an intention-to-treat comparison, so the arm's own
    # behaviour never decides which tasks count. Dropping "inert" would score the arm only where it
    # chose to delegate, which measures the choice; keeping it dilutes the effect toward zero,
    # which is the conservative direction, and `delegated_in` is what says how much.
    assert [c.task_id for c in report.comparisons] == ["inert", "real-a", "real-b"]
    inert = next(c for c in report.comparisons if c.task_id == "inert")
    assert (inert.delegated_in, inert.repeats) == (0, MINIMUM_REPEATS)


def test_a_task_missing_an_arm_is_incomplete_rather_than_absent() -> None:
    """A one-armed task is a hole, and a merely absent hole is invisible in a denominator."""
    runs = [
        *_pair("both"),
        *_runs("lonely", ARM, quality=1.0, tokens=1, seconds=1.0, delegated=True),
    ]

    report = compare_arms(runs, arm=ARM)

    assert report.incomplete == ["lonely"]
    assert report.compared == 1


def test_too_few_repeats_is_incomplete_on_either_side() -> None:
    """Below the floor a median is one observation wearing a robust-sounding name."""
    thin_arm = compare_arms([*_pair("ok"), *_pair("thin", repeats=MINIMUM_REPEATS - 1)], arm=ARM)
    assert thin_arm.incomplete == ["thin"]

    # And the floor binds on the baseline independently, not only on the arm under test.
    mixed = [
        *_pair("ok"),
        *_runs(
            "lopsided",
            BASELINE_ARM,
            quality=1.0,
            tokens=10,
            seconds=1.0,
            delegated=False,
            repeats=1,
        ),
        *_runs("lopsided", ARM, quality=1.0, tokens=10, seconds=1.0, delegated=True),
    ]
    assert compare_arms(mixed, arm=ARM).incomplete == ["lopsided"]


def test_an_empty_comparison_raises_rather_than_reporting_no_effect() -> None:
    """The vacuous pass, refused — and the message names which drop emptied the set."""
    with pytest.raises(NoComparableTask, match="contaminated"):
        compare_arms(_pair("dirty", base_delegated=True), arm=ARM)

    with pytest.raises(ValueError, match="proves nothing"):
        compare_arms([], arm=ARM)

    with pytest.raises(ValueError, match="same arm"):
        compare_arms(_pair("x"), arm=BASELINE_ARM)


def test_a_zero_baseline_drops_that_axis_and_keeps_the_others() -> None:
    """A turn that failed before billing is a real observation and an impossible ratio.

    Dropping the whole task would lose its quality and wall-clock evidence over an artefact of one
    axis; letting the ratio through would put an infinity into a median and make the cost answer
    about the one broken run.
    """
    report = compare_arms(_pair("free", base_tokens=0, arm_tokens=0), arm=ARM)

    assert report.compared == 1
    assert report.median_token_ratio is None
    assert report.median_wall_clock_ratio is not None
    assert report.comparisons[0].token_delta == 0


def test_the_quality_verdict_comes_from_the_shared_noise_floor() -> None:
    """Quality is `compare_tool_utility`'s axis, epsilon and vocabulary — not a second scale."""
    report = compare_arms(
        [
            *_pair("better", base_quality=0.0, arm_quality=1.0),
            *_pair("worse", base_quality=1.0, arm_quality=-1.0),
        ],
        arm=ARM,
    )

    verdicts = {c.task_id: c.verdict for c in report.comparisons}
    assert verdicts == {"better": "helped", "worse": "hurt"}
    assert report.quality.helped == ["better"]
    assert report.quality.hurt == ["worse"]
    # A `fabricated` arm answer scores below an `unserved` baseline, which is what makes the sum
    # negative rather than neutral: delegation's own error class has to be able to show up.
    assert report.quality.net_delta == pytest.approx(-1.0)


def test_repeats_are_taken_at_the_median_so_one_outlier_cannot_carry_the_cost() -> None:
    """Why repeats exist: the timed-out run is survived rather than averaged in."""
    runs = [
        ArmRun(
            task_id="t",
            arm=ARM,
            quality=1.0,
            billed_tokens=tokens,
            wall_clock_seconds=seconds,
            delegated=True,
        )
        for tokens, seconds in ((500, 10.0), (520, 11.0), (50_000, 600.0))
    ]

    [aggregate] = aggregate_runs(runs)

    assert aggregate.repeats == 3
    assert aggregate.billed_tokens == 520, "a mean would read 17,006 and describe the outlier"
    assert aggregate.wall_clock_seconds == 11.0
    assert aggregate.delegated_in == 3


def test_delegated_in_counts_repeats_rather_than_collapsing_to_a_bool() -> None:
    """A count, not a bool: "delegated in one repeat of three" is its own fact."""
    runs = [
        ArmRun(
            task_id="t",
            arm=BASELINE_ARM,
            quality=1.0,
            billed_tokens=10,
            wall_clock_seconds=1.0,
            delegated=delegated,
        )
        for delegated in (False, False, True)
    ]

    [aggregate] = aggregate_runs(runs)

    assert aggregate.delegated_in == 1
    # And one non-compliant repeat is enough to contaminate the pair: the baseline is not a
    # baseline in a third of its observations, which no median over quality would reveal.
    paired = [*runs, *_runs("t", ARM, quality=1.0, tokens=5, seconds=1.0, delegated=True)]
    with pytest.raises(NoComparableTask):
        compare_arms(paired, arm=ARM)


def test_a_third_arm_is_ignored_rather_than_refused() -> None:
    """One recording of a three-arm campaign answers both comparisons without being re-run."""
    runs = [
        *_pair("t"),
        *_runs("t", "helper-routed", quality=1.0, tokens=300, seconds=9.0, delegated=True),
    ]

    against_helper = compare_arms(runs, arm=ARM)
    against_routed = compare_arms(runs, arm="helper-routed")

    assert against_helper.compared == against_routed.compared == 1
    assert against_helper.median_token_ratio == pytest.approx(0.5)
    assert against_routed.median_token_ratio == pytest.approx(0.3)


def test_quality_stays_an_observation_on_an_even_number_of_repeats() -> None:
    """The averaging the module forbids, reproduced at the repeat count that produces it.

    `MINIMUM_REPEATS` is documented as a floor rather than a target, so four repeats is ordinary —
    and `statistics.median` returns the *mean of the two middle values* on an even-sized group. On
    the five-point verdict scale that is exactly the "number that names no verdict" the module's own
    docstring rejects: `served` (1.0) beside `unserved` (0.0) would aggregate to 0.5, which is
    `partial`'s score, asserted about a pair of runs where neither arm was ever partial.

    Every other case in this file uses three *identical* runs, so none of them can tell `median`
    from `median_low` — which is why this one varies the values and takes an even count.
    """
    runs = [
        ArmRun(
            task_id="t",
            arm=ARM,
            quality=quality,
            billed_tokens=100,
            wall_clock_seconds=1.0,
            delegated=True,
        )
        for quality in (0.0, 0.0, 1.0, 1.0)
    ]

    [aggregate] = aggregate_runs(runs)

    assert aggregate.repeats == 4
    assert aggregate.quality in {0.0, 1.0}, "quality must name a verdict, not the midpoint of two"
    assert aggregate.quality == 0.0, "median_low takes the lower of the two middles"


def test_the_cost_axes_keep_the_midpoint_on_an_even_number_of_repeats() -> None:
    """The other half: tokens and seconds are continuous, so a midpoint is a real figure.

    Stated as its own case so that someone switching the cost axes to `median_low` for symmetry has
    to change a test that says why the asymmetry is deliberate.
    """
    runs = [
        ArmRun(
            task_id="t",
            arm=ARM,
            quality=1.0,
            billed_tokens=tokens,
            wall_clock_seconds=seconds,
            delegated=True,
        )
        for tokens, seconds in ((100, 1.0), (200, 3.0))
    ]

    [aggregate] = aggregate_runs(runs)

    assert aggregate.billed_tokens == 150.0
    assert aggregate.wall_clock_seconds == 2.0


def test_an_arm_that_delegated_in_some_repeats_is_reported_with_its_compliance() -> None:
    """Compliance is a number beside the result, never a filter in front of it.

    `ArmAggregate.delegated_in` is a count because "delegated in one repeat of three is a different
    fact from either extreme and is the shape a behavioural arm actually produces". Two earlier
    versions of this comparison threw that away: one collapsed it to `if not delegated_in` and
    credited the task outright, the other required every repeat and dropped anything less. Both
    conditioned on the treatment; the second also made the instrument refuse corpora a real run
    produces (a Monte-Carlo puts the per-repeat delegation needed for an even chance of any report
    at ~87.4%, rising with the repeat count).

    So the task is compared and its compliance travels with it.
    """
    mixed = [
        *_runs("mixed", BASELINE_ARM, quality=0.5, tokens=10_000, seconds=60.0, delegated=False),
        ArmRun(
            task_id="mixed",
            arm=ARM,
            quality=1.0,
            billed_tokens=2_000,
            wall_clock_seconds=20.0,
            delegated=True,
        ),
        *_runs(
            "mixed",
            ARM,
            quality=0.5,
            tokens=10_000,
            seconds=60.0,
            delegated=False,
            repeats=MINIMUM_REPEATS - 1,
        ),
    ]

    report = compare_arms([*_pair("solid"), *mixed], arm=ARM)

    assert report.partially_delegated == ["mixed"]
    assert "mixed" not in report.undelegated, "it did delegate, so this is not that case"
    compared = next(c for c in report.comparisons if c.task_id == "mixed")
    assert (compared.delegated_in, compared.repeats) == (1, MINIMUM_REPEATS), (
        "a reader must be able to see that this task's result is one delegating run in three"
    )


def test_an_arm_that_declines_or_fails_the_hard_tasks_cannot_report_that_it_helped() -> None:
    """The selection effect, in the two shapes the bound that preceded this missed.

    A share bound over the *surviving* tasks was added to stop "helped everywhere, 60% cheaper,
    33% faster" over one task of eight. It counted only the tasks the arm *declined*, so both of
    these reproduced that headline with the guard green: an arm that crashed or timed out on the
    hard seven (`incomplete`, excluded from the bound's denominator), and an arm that ran, lost
    badly, and completed 2 of 3 repeats each (`incomplete` again, on a repeat-count technicality).

    Under intention-to-treat there is nothing to bound, because nothing is dropped for the arm's
    behaviour: a task the arm declined is compared and dilutes toward zero, and a task the arm lost
    is compared and counts against it. Both arms of this test are the headline the old guard let
    through.
    """
    helped_once = list(_pair("t1", base_quality=0.0, arm_quality=1.0, arm_tokens=400))
    declined = [run for index in range(2, 9) for run in _pair(f"t{index}", arm_delegated=False)]

    declining = compare_arms([*helped_once, *declined], arm=ARM)

    assert declining.compared == 8, "a task the arm declined is compared, not struck out"
    assert declining.median_token_ratio is not None
    assert declining.median_token_ratio > 0.4, (
        "the cost win must be diluted by the seven tasks where nothing was delegated, not read "
        "off the one task where it was"
    )

    lost = [
        run
        for index in range(2, 9)
        for run in _pair(f"t{index}", base_quality=1.0, arm_quality=0.0, arm_tokens=9_000)
    ]
    losing = compare_arms([*helped_once, *lost], arm=ARM)

    assert losing.compared == 8
    assert [c.task_id for c in losing.comparisons if c.verdict == "hurt"], (
        "seven tasks the arm lost must reach the report as losses"
    )
