"""What a run says about itself: relaxable wall-clock caps, and the tests that never ran.

A timed-out test proves nothing about the assertions it never reached, and a skipped Postgres test
proves nothing at all; `tests/conftest.py` reports both. A `@pytest.mark.timeout` marker overrides
`--timeout`, so `PYTEST_TIMEOUT_SCALE` is the only way to relax it on a contended machine. Both are
exercised through a real pytest session importing the real hook.
"""

from pathlib import Path

import pytest
from _pytest.config import UsageError

from tests.conftest import timeout_scale

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)

# Imports the hook under test by name, so what runs in the throwaway session is the shipped one.
_CONFTEST = f"""
import sys

sys.path.insert(0, {_REPO_ROOT!r})

from tests.conftest import (  # noqa: F401
    pytest_collection_modifyitems,
    pytest_terminal_summary,
)
"""

# Sleeps past its own 1 s marker but not past four times it, so the marker alone decides the
# outcome and reaching the marker is the only thing the scale can be credited for.
_SLOW_TEST = """
import time

import pytest


@pytest.mark.timeout(1)
def test_slower_than_its_marker() -> None:
    time.sleep(1.6)
"""


def _write_suite(pytester: pytest.Pytester) -> None:
    """Lay down a one-test suite whose conftest is the real hook and whose ini cap is generous."""
    pytester.makeconftest(_CONFTEST)
    # A 30 s session default, deliberately far above the marker: only the marker can fail this
    # test, so a scale that reached the default but not the marker would still show red.
    pytester.makeini("[pytest]\ntimeout = 30\n")
    pytester.makepyfile(test_slow=_SLOW_TEST)


def test_a_marker_alone_still_fails_a_test_that_outruns_it(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unscaled, the marker still fails a test that outruns it, in its own report section.

    The timeouts catch real hangs such as a runaway xTB optimisation, and the run must say it was a
    timeout so it is not read as a numerical failure.
    """
    monkeypatch.delenv("PYTEST_TIMEOUT_SCALE", raising=False)
    _write_suite(pytester)
    result = pytester.runpytest_subprocess("-p", "no:randomly")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        ["*timeouts — these assertions never ran*", "TIMEOUT test_slow.py::test_slower_than_*"]
    )


def test_the_scale_relaxes_a_marker_no_command_line_flag_can_reach(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--timeout` cannot lift a marker; `PYTEST_TIMEOUT_SCALE` can.

    The same 1 s marker over 1.6 s of work passes only because the scale reaches the marker itself.
    """
    monkeypatch.setenv("PYTEST_TIMEOUT_SCALE", "4")
    _write_suite(pytester)
    pytester.runpytest_subprocess("-p", "no:randomly").assert_outcomes(passed=1)


def test_scaling_a_marker_keeps_the_timeout_method_it_carried(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`method="thread"` survives the rewrite of a scaled marker.

    The `signal` method cannot interrupt a thread blocked in `temporalio`'s Rust core. The two
    methods fail differently: `thread` dumps stacks and calls `os._exit(1)`, leaving no test
    outcome, so the assertion is on the absence of a normal outcome.
    """
    monkeypatch.setenv("PYTEST_TIMEOUT_SCALE", "2")
    pytester.makeconftest(_CONFTEST)
    pytester.makeini("[pytest]\ntimeout = 30\n")
    pytester.makepyfile(
        test_thread="""
import time

import pytest


@pytest.mark.timeout(1, method="thread")
def test_outruns_even_the_scaled_cap() -> None:
    time.sleep(6)
"""
    )
    result = pytester.runpytest_subprocess("-p", "no:randomly")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*Timeout*"])
    with pytest.raises(ValueError, match="terminal summary report not found"):
        result.parseoutcomes()  # `os._exit` ended the session before any summary was written


def test_the_scale_defaults_to_one_and_refuses_nonsense(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset means unchanged; a typo fails loudly rather than silently removing every cap.

    `0` and a negative are refused rather than clamped, because either would read as "no timeout
    at all" — turning the knob that exists to keep the caps usable into one that deletes them.
    """
    monkeypatch.delenv("PYTEST_TIMEOUT_SCALE", raising=False)
    assert timeout_scale() == 1.0

    monkeypatch.setenv("PYTEST_TIMEOUT_SCALE", "2.5")
    assert timeout_scale() == 2.5

    for bad in ("", "lots", "0", "-1"):
        monkeypatch.setenv("PYTEST_TIMEOUT_SCALE", bad)
        with pytest.raises(UsageError, match="PYTEST_TIMEOUT_SCALE"):
            timeout_scale()


def test_the_knob_does_not_wear_the_products_config_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The knob does not use the `CHEMCLAW_*` prefix.

    That prefix claims a `Settings` field, and machine load has nothing to do with a deployment;
    prose-contract rule 7 fails any `CHEMCLAW_*` key that is not one.
    """
    # Cleared first, since the timeout banner tells a user to set it and `timeout_scale()` would
    # then read it.
    monkeypatch.delenv("PYTEST_TIMEOUT_SCALE", raising=False)
    monkeypatch.setenv("CHEMCLAW_TEST_TIMEOUT_SCALE", "8")
    assert timeout_scale() == 1.0, "the product prefix must not name a pytest knob"


def test_a_run_says_how_many_postgres_backed_tests_never_ran(pytester: pytest.Pytester) -> None:
    """A run reports how many Postgres-backed tests never ran, counted by the run itself.

    Driven through a real session skipping with the real `tests/pg.py` marker, because the epilogue
    matches on that reason string.
    """
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(
        test_needs_pg="""
import pytest


@pytest.mark.parametrize("case", [1, 2, 3])
def test_needs_a_database(case: int) -> None:
    pytest.skip("Postgres unavailable (start it: sudo dockerd; make up): connection refused")


def test_needs_nothing() -> None:
    assert True
"""
    )
    result = pytester.runpytest_subprocess("-p", "no:randomly")
    result.assert_outcomes(passed=1, skipped=3)
    result.stdout.fnmatch_lines(
        ["*Postgres-backed tests did not run*", "3 tests were skipped because Postgres*"]
    )


def test_a_run_with_a_database_says_nothing_about_skips(pytester: pytest.Pytester) -> None:
    """The other side, so the epilogue cannot be satisfied by printing the banner unconditionally.

    A skip for any other reason is not this warning: the section is about one specific thing being
    unreachable, and a banner that appeared on every run would be read as noise and stop working.
    """
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(
        test_other_skip="""
import pytest


def test_skipped_for_another_reason() -> None:
    pytest.skip("tblite shared library is not where the probe expects")
"""
    )
    result = pytester.runpytest_subprocess("-p", "no:randomly")
    result.assert_outcomes(skipped=1)
    assert "Postgres-backed tests did not run" not in result.stdout.str()
