"""A job this deployment cannot run is withheld, and refused on the paths that reach it anyway.

`republish_calculations` with `CHEMCLAW_RESULT_SINKS` empty can only fail, so its manifest declares
`unavailable_reason`. Edges: withheld at the shipped default, bound once a sink is named, still
declared either way, refused at launch for a caller naming it by string, and withheld even when an
earlier build in the process registered it. Uses the shipped `results` bundle, not a fixture.
"""

import pytest

from chemclaw.agent.chemclaw_agent import (
    available_tool_names,
    declared_tool_names,
    withheld_tool_names,
)
from chemclaw.connectors.jobs import ConnectorJobError, prepare_job_launch, unavailable_reason
from chemclaw.connectors.registry import find_job, job_names, job_tools, withheld_job_names
from chemclaw.core import tool_registry
from chemclaw.core.config import settings
from tests.surface import surface

_JOB = "republish_calculations"


def _publishing_nowhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped default, stated rather than assumed: no sink is named."""
    monkeypatch.setattr(settings, "result_sinks", "")


def test_the_default_deployment_binds_no_republish_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three readings of "bound", because each is a different consumer and any one is paid for.

    The generator, the union the validators, the verifier and the live-probe expectation resolve
    against, and the surface a compiled graph binds.
    """
    _publishing_nowhere(monkeypatch)
    assert _JOB in withheld_job_names()
    assert _JOB not in {tool.__name__ for tool in job_tools()}
    assert _JOB not in available_tool_names()
    assert _JOB not in surface(None).tool_names


def test_a_withheld_job_is_still_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    """Withheld is not deleted: a probe, a skill or a template naming it still names a real job."""
    _publishing_nowhere(monkeypatch)
    assert _JOB in job_names()
    assert _JOB in declared_tool_names()


def test_naming_a_sink_binds_the_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half: the launcher follows the sink on, with no other edit."""
    monkeypatch.setattr(settings, "result_sinks", "postgres")
    assert unavailable_reason(find_job(_JOB)[1]) is None
    assert _JOB not in withheld_job_names()
    assert _JOB in {tool.__name__ for tool in job_tools()}
    assert _JOB in available_tool_names()


def test_a_launch_that_names_the_job_by_string_is_refused_before_it_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A template step reaches `prepare_job_launch` without the launcher filter; it refuses there.

    Before authorization, too: no entitlement makes a job runnable that has nowhere to deliver,
    so the reason a caller reads is the configuration and not a role they lack.
    """
    _publishing_nowhere(monkeypatch)
    connector, job = find_job(_JOB)
    with pytest.raises(ConnectorJobError, match="CHEMCLAW_RESULT_SINKS"):
        prepare_job_launch(connector, job, {})


def test_a_launcher_registered_under_another_configuration_is_still_withheld(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launcher registered under another configuration is still withheld.

    The registry only grows, so the surface is decided by the rule, not by what an earlier build in
    the process registered.
    """
    monkeypatch.setattr(settings, "result_sinks", "postgres")
    (launcher,) = [tool for tool in job_tools() if tool.__name__ == _JOB]
    monkeypatch.setitem(tool_registry._REGISTRY, _JOB, launcher)
    _publishing_nowhere(monkeypatch)
    assert _JOB in withheld_tool_names()
    assert _JOB not in available_tool_names()
    assert _JOB not in surface(None).tool_names
