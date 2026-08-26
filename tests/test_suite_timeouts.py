"""What a run says about itself: relaxable wall-clock caps (T11), and the tests that never ran.

Both halves are about a run's headline number being believed without the things that qualify it: a
timed-out test proves nothing about the assertions it never reached, and a skipped Postgres test
proves nothing at all. `tests/conftest.py` owns both, and both are exercised here the only way a
terminal-summary hook can be — through a real session.

`pyproject.toml` caps every test at 180 s and two files tighten that further with
`@pytest.mark.timeout(...)`. A marker overrides `--timeout` and `PYTEST_TIMEOUT`, so before
`PYTEST_TIMEOUT_SCALE` existed the tests with the tightest caps were precisely the ones no
command line could relax — and a contended machine turned them red with their assertions never
run. Two reviewers read that red as a numerical failure, and a hardening campaign spent hours
against the baseline it produced.

The scaling is exercised the only way it can be believed: a real pytest session, with a real
marker, importing the real hook from `tests/conftest.py` rather than a copy of it.
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
    """Unscaled, the marker bites — the behaviour the markers exist for, deliberately kept.

    Deleting the timeouts would have been the easy answer and the wrong one: a runaway xTB
    optimisation hanging CI indefinitely is a real failure they catch.

    The run must also *say* it was a timeout, in its own section. `FAILED … - Failed: Timeout
    (>180.0s) from pytest-timeout` in the short summary was read as a numerical failure twice, and
    a timed-out test is not weak evidence about the code — it is none, because the assertions never
    ran.
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
    """The whole point: `--timeout` cannot lift a marker, `PYTEST_TIMEOUT_SCALE` can.

    The same suite, the same 1 s marker, the same 1.6 s of work — passing only because the scale
    was applied to the marker itself. Removing the `_apply_timeout_scale(config, items)` call from
    `pytest_collection_modifyitems` fails this and leaves the rest of the suite green.
    """
    monkeypatch.setenv("PYTEST_TIMEOUT_SCALE", "4")
    _write_suite(pytester)
    pytester.runpytest_subprocess("-p", "no:randomly").assert_outcomes(passed=1)


def test_scaling_a_marker_keeps_the_timeout_method_it_carried(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`method="thread"` must survive the rewrite, or the Temporal hang guard quietly reverts.

    `_get_item_settings` reads timeout *and* method off the one closest marker, so a scaled
    replacement that dropped `**kwargs` would return every Temporal-backed module to the `signal`
    method — which cannot interrupt a thread blocked in `temporalio`'s Rust core, the exact
    28-minute silent hang the method was switched for. Nothing would show it locally: those
    modules skip without a Temporal server.

    Observable because the two methods fail differently. `thread` dumps every stack and calls
    `os._exit(1)`, so the session ends with no test outcome at all; `signal` reports an ordinary
    failure. Asserting on the absence of a normal outcome is what distinguishes them.
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
    """`CHEMCLAW_*` is a claim: the key comes from the one `pydantic-settings` config.

    This one does not and must not — how loaded the machine is has nothing to do with a deployment,
    and `core/config/`'s parity test requires every field to appear in `.env.example`. Under the old
    name the claim was false and only *invisible*: prose-contract rule 7 fails any `CHEMCLAW_*` key
    that is not a `Settings` field, and it reads the operator corpus, which `tests/README.md` is
    outside of. Measured before the rename — one sentence about it in `README.md`, the natural place
    to tell someone how to run the suite on a loaded machine — `make prose-validate` failed with
    "names CHEMCLAW_TEST_TIMEOUT_SCALE, which is not a Settings field".
    """
    # **The real knob is cleared first, and leaving it out made this test fail for exactly the
    # person the suite tells to set it.** `PYTEST_TIMEOUT_SCALE` is what the timeout banner
    # prescribes on a loaded machine ("re-run with PYTEST_TIMEOUT_SCALE=4"), and with it set in the
    # environment `timeout_scale()` correctly returns 4.0 — so the assertion below failed on a
    # reading that had nothing to do with the prefix it is about. Found by following that advice.
    monkeypatch.delenv("PYTEST_TIMEOUT_SCALE", raising=False)
    monkeypatch.setenv("CHEMCLAW_TEST_TIMEOUT_SCALE", "8")
    assert timeout_scale() == 1.0, "the product prefix must not name a pytest knob"


def test_a_run_says_how_many_postgres_backed_tests_never_ran(pytester: pytest.Pytester) -> None:
    """The count of what an unreachable database took away is measured, never written down.

    `CLAUDE.md` warns that a green local run can mean the durable layer never executed, and it
    stated the size of that as a number — "~157 Postgres tests" — which was stale by ~38% in the
    direction that understates the risk it exists to warn about (216 measured). A count in prose
    describes the suite on the day someone counted it; this one is produced by the run that is
    reporting it, which is what `D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose` asks
    for wherever a count is worth having at all.

    Driven through a real session that skips with the real marker `tests/pg.py` writes, because the
    epilogue matches on that reason string and a hand-called helper would only prove the matcher
    agrees with itself.
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
