"""The suite's own wall-clock caps are relaxable on a loaded machine (T11).

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
    monkeypatch.setenv("CHEMCLAW_TEST_TIMEOUT_SCALE", "8")
    assert timeout_scale() == 1.0, "the product prefix must not name a pytest knob"
