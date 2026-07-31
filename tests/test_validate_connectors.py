"""`make connector-validate` — the CI gate, tested for the failures it exists to catch.

A validator with no test is a validator nobody knows still works: it passes on the shipped bundles
either because it is correct or because it stopped checking, and those look identical from the
outside. So each test here asserts one *rejection*, against a manifest built in the test rather than
a shipped one — the shipped bundles are covered by the gate itself being green.

The two checks below are the ones no per-file schema can make, which is the whole reason the script
exists on top of pydantic's own validation:

- a job that cannot be *built* (an unresolvable `params_model`), because that failure would
  otherwise surface the first time a chemist called the tool;
- an `inline_wait_seconds` at or beyond the turn timeout, which needs the deployment's config and so
  is invisible to the manifest that declares it.
"""

import pytest

from chemclaw.cli.validate_connectors import _job_problems
from chemclaw.connectors.manifest import ConnectorManifest
from chemclaw.core.config import settings

_MANIFEST = {
    "name": "probe",
    "description": "A probe bundle used to exercise the validator.",
    "endpoint": {
        "transport": "http",
        "url": "http://127.0.0.1:8899/mcp",
        "auth": {"mode": "none"},
        "tools": ["probe_tool"],
        "read_only": ["probe_tool"],
    },
}

_JOB = {
    "name": "run_probe",
    "workflow": "ProbeWorkflow",
    "summary": "Run the probe.",
}


def _manifest(**job_overrides: object) -> ConnectorManifest:
    """A valid probe manifest carrying one job with `job_overrides` applied."""
    return ConnectorManifest.model_validate({**_MANIFEST, "jobs": [{**_JOB, **job_overrides}]})


def test_a_job_that_cannot_be_built_is_reported_not_deferred_to_run_time() -> None:
    """An unresolvable `params_model` must fail in CI, not on the first tool call."""
    problems = _job_problems(_manifest(params_model="nowhere.at.all:Model"))
    assert any("cannot be built" in problem and "run_probe" in problem for problem in problems)


def test_an_inline_wait_beyond_the_turn_timeout_is_refused() -> None:
    """The check the manifest cannot make itself, because the turn timeout is the deployment's.

    A wait at or past `service_turn_timeout_seconds` is a job whose fast path can never win: the
    turn is killed before the wait returns, so *every* call looks like a timeout instead of like
    the deferral it should have been — a bug that only shows up under load, in production.
    """
    problems = _job_problems(_manifest(inline_wait_seconds=settings.service_turn_timeout_seconds))
    assert any("turn timeout" in problem for problem in problems)


def test_a_wait_comfortably_inside_the_turn_is_accepted() -> None:
    """The passing case, so the check cannot be satisfied by rejecting everything."""
    assert _job_problems(_manifest(inline_wait_seconds=5)) == []


def test_a_job_with_no_inline_budget_is_not_checked_against_the_turn() -> None:
    """`inline_wait_seconds` is opt-in: a plain durable job never waits, so nothing to bound."""
    assert _job_problems(_manifest()) == []


def test_the_shipped_bundles_pass_their_own_gate() -> None:
    """What CI actually runs, asserted here too so a broken bundle fails the suite, not just `make`.

    Discovery rather than the enabled set: a bundle that is broken while disabled is one nobody can
    turn on, and finding that out at enable time is exactly what this gate prevents.
    """
    from chemclaw.cli.validate_connectors import validate_connectors

    assert validate_connectors() == []


@pytest.mark.parametrize("budget", [0, -1])
def test_a_nonpositive_wait_is_refused_by_the_manifest_itself(budget: int) -> None:
    """Bounded below by the schema, not the script: "wait zero seconds" is a contradiction.

    Kept here beside the upper bound so the two ends of the same field are read together.
    """
    with pytest.raises(ValueError, match="inline_wait_seconds"):
        _manifest(inline_wait_seconds=budget)
