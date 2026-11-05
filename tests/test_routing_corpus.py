"""The routing corpus holds only cases whose wording points at the wrong profile.

`data/evals/routing/profile-routing.yaml`. A case is a mismatch on the surface, not on taste: the
profile its wording suggests binds none of the tools the probe expects, and the profile it needs
binds at least one. Checked against the shipped profile files, so a profile that grows a tool turns
a case honest and fails here. Whether answers
differ by profile is a live measurement this file does not make.
"""

from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel, ConfigDict, Field

from chemclaw.agent.chemclaw_agent import available_tool_names
from chemclaw.agent.profile_discovery import load_profiles
from chemclaw.agent.profiles import DEFAULT_PROFILE, get_profile, registered_profile_names
from chemclaw.evals.probe import Probe
from tests.test_probe_coverage import _probes

CORPUS = Path(__file__).resolve().parents[1] / "data" / "evals" / "routing" / "profile-routing.yaml"


class RoutingCase(BaseModel):
    """One question, the profile its wording suggests, and the profile an answer needs."""

    model_config = ConfigDict(extra="forbid")

    worded_like: str = Field(min_length=1)
    needs: str = Field(min_length=1)
    why: str = Field(min_length=1)
    probe: Probe


class RoutingCorpus(BaseModel):
    """The file: `cases:` and nothing else."""

    model_config = ConfigDict(extra="forbid")

    cases: list[RoutingCase] = Field(min_length=1)


@pytest.fixture(scope="module", autouse=True)
def _profiles_loaded() -> None:
    """The shipped profile files, registered so `get_profile` resolves them."""
    load_profiles()


def _cases() -> list[RoutingCase]:
    """Every case, parsed through the models above."""
    return RoutingCorpus.model_validate(yaml.safe_load(CORPUS.read_text("utf-8"))).cases


def _binds(profile: str, tool: str) -> bool:
    """Whether `profile` puts `tool` on its surface; an unnarrowed profile binds every tool."""
    names = get_profile(profile).tool_names
    return names is None or tool in names


def test_every_case_names_two_different_shipped_profiles() -> None:
    """Both names resolve, and a case whose wording is honest does not belong here."""
    shipped = set(registered_profile_names())
    assert len(shipped) > 1, "no profile files were discovered, so nothing below is checked"
    for case in _cases():
        assert case.worded_like in shipped, case.probe.id
        assert case.worded_like != DEFAULT_PROFILE.name, case.probe.id
        assert case.needs in shipped, case.probe.id
        assert case.worded_like != case.needs, case.probe.id


def test_the_suggested_profile_cannot_serve_the_question_and_the_needed_one_can() -> None:
    """The mismatch is on the tool surface: what the wording suggests binds none of the tools."""
    for case in _cases():
        expected = case.probe.expects_tools
        assert expected, f"{case.probe.id} expects no tool, so its mismatch cannot be checked"
        bound = [tool for tool in expected if _binds(case.worded_like, tool)]
        assert not bound, f"{case.probe.id}: {case.worded_like} binds {bound}, so it can serve it"
        assert any(_binds(case.needs, tool) for tool in expected), (
            f"{case.probe.id}: {case.needs} binds none of {expected}"
        )


def test_every_expected_tool_is_on_the_agent_surface() -> None:
    """A misspelt tool would pass the mismatch check for every profile that narrows."""
    surface = available_tool_names()
    for case in _cases():
        assert set(case.probe.expects_tools) <= surface, (
            case.probe.id,
            sorted(set(case.probe.expects_tools) - surface),
        )


def test_every_suggested_profile_is_covered_and_ids_are_unique_across_corpora() -> None:
    """Each narrowing profile is the misleading one at least once; no id collides with a probe's."""
    cases = _cases()
    narrowing = {name for name in registered_profile_names() if get_profile(name).tool_names}
    assert narrowing <= {case.worded_like for case in cases}
    ids = [case.probe.id for case in cases]
    assert len(ids) == len(set(ids))
    assert not set(ids) & {probe.id for probe in _probes()}
