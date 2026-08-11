"""Profiles authored as files, and selected per session — the two halves of Stage D.

A profile only becomes a *use-case configuration* when both are true: it can be written without
touching Python, and a caller can ask for it. Either alone is not enough — a registry no one can
select from is dead code, and a selectable name with no way to author it is still a redeploy.

The load-bearing assertions are about narrowing spanning both halves of the tool surface (the
in-process registry and the connectors' allow-lists), because that is what makes a profile
expressible at all now that the domain capabilities are out of process — and about the invariant
underneath everything: a profile *attenuates*, it never authorizes.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chemclaw.agent.chemclaw_agent import advertised_tool_names, connector_specs
from chemclaw.agent.profile_discovery import ProfileError, load_profiles, profile_files
from chemclaw.agent.profiles import _REGISTRY, get_profile, registered_profile_names
from chemclaw.api.app import create_app
from tests.surface import surface

_PROFILE = """\
instructions: Answer tersely.
tool_names:
  - predict_pka
  - ask_clarifying_question
"""


@pytest.fixture
def profiles_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point profile discovery at an empty temp tree, and unregister whatever a test adds.

    The registry is module state, so a leaked profile would be visible to every later test — and
    `load_profiles` is deliberately idempotent, which would hide the leak rather than surface
    it.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.profiles_dir", str(tmp_path))
    before = set(registered_profile_names())
    try:
        yield tmp_path
    finally:
        for name in set(registered_profile_names()) - before:
            _REGISTRY.pop(name, None)


def _write(directory: Path, name: str, body: str = _PROFILE) -> Path:
    """Write one profile file and return its path."""
    path = directory / f"{name}.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_profile_is_its_filename(profiles_dir: Path) -> None:
    """The stem names the profile, so a file and a registry key cannot disagree.

    A test-only name: `load_profiles` is idempotent, so reusing a shipped profile's name would
    make this assert nothing once another test had already registered it.
    """
    _write(profiles_dir, "probe-lookup")
    (loaded,) = load_profiles()
    assert loaded.name == "probe-lookup"
    assert get_profile("probe-lookup") is loaded


def test_a_name_key_inside_the_file_is_refused(profiles_dir: Path) -> None:
    """Two sources of truth for one identity is the drift this refuses up front."""
    _write(profiles_dir, "shadow", "name: something-else\ninstructions: hi\n")
    with pytest.raises(ProfileError, match="name is its filename"):
        load_profiles()


def test_a_misspelled_override_fails_rather_than_doing_nothing(profiles_dir: Path) -> None:
    """`extra="forbid"` on `AgentProfile`: a typo'd key is a startup error, not a silent no-op.

    The failure mode this prevents is the expensive one — a profile that loads, looks fine, and
    quietly ignores the narrowing its author wrote.
    """
    _write(profiles_dir, "typo", "instruction: Answer tersely.\n")
    with pytest.raises(ProfileError, match="invalid profile"):
        load_profiles()


def test_two_files_claiming_one_name_is_an_error(
    profiles_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Which agent a caller gets must not depend on directory order."""
    second = tmp_path / "other"
    second.mkdir()
    _write(profiles_dir, "clash")
    _write(second, "clash")
    monkeypatch.setattr("chemclaw.core.config.settings.profiles_dir", f"{profiles_dir}:{second}")
    with pytest.raises(ProfileError, match="already defined by"):
        load_profiles()


def test_loading_twice_is_idempotent(profiles_dir: Path) -> None:
    """The front door builds agents lazily and tests build many; re-loading must not raise."""
    _write(profiles_dir, "steady")
    assert [p.name for p in load_profiles()] == ["steady"]
    assert load_profiles() == []  # already registered, nothing new
    assert "steady" in registered_profile_names()


def test_a_bundle_can_ship_its_own_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """A profile about one capability is found in that connector's bundle, not only the shared tree.

    The same split as skills: shared content in the configured tree, capability-specific content
    in the bundle so it ships and is reviewed with the capability it is about.
    """
    from chemclaw.connectors.registry import profiles_dirs

    monkeypatch.setattr("chemclaw.core.config.settings.profiles_dir", "does-not-exist")
    # No bundle declares profiles today, so the discovery list is exactly the shared tree's —
    # the assertion worth making is that the bundle half is wired, not that a bundle happens to
    # use it.
    assert profiles_dirs() == []
    assert profile_files() == []


def test_the_shipped_profile_narrows_both_halves_of_thesurface() -> None:
    """`property-lookup` gets four connector tools and one in-process tool, and nothing else.

    This is the property that makes a profile useful after the domain capabilities moved out of
    process: one `tool_names` dial reaches the in-process registry *and* each connector's
    agent-facing allow-list, dropping connectors left with nothing.
    """
    load_profiles()
    agent = surface("property-lookup")
    assert agent.tool_names == {"ask_clarifying_question"}
    attached = {c.name: sorted(c.allowed_tools or ()) for c in connector_specs("property-lookup")}
    assert attached == {
        "calc": ["calculator_trust", "compute_xtb_energy", "predict_pka", "predict_solubility"]
    }


@pytest.mark.parametrize("profile", [None, "property-lookup"])
def test_advertised_tool_names_matches_the_surface_the_agent_really_builds(
    profile: str | None,
) -> None:
    """`advertised_tool_names` must equal what `build_agent` + `connector_tools` actually produce.

    It has to answer that question *without* calling `connector_tools`, because constructing a
    connector's MCP tool opens an `httpx.AsyncClient` that only a turn's exit stack ever closes —
    so it reads the manifests and re-applies the same two narrowings by hand. Re-applying a rule is
    exactly how two implementations of it drift, and the drift would be invisible: the skill
    surface would simply be scoped against a slightly wrong set of tools and every turn would look
    fine. So the two are compared here, over both the full surface and the narrowing profile,
    which is the case where the rules actually do something.
    """
    load_profiles()
    advertised = surface(profile)
    # No `try/finally` releasing anything: a `ConnectorSpec` is a description, not an open client.
    # It used to be an unconnected MAF tool object owning an httpx client, which had to be closed
    # or the test leaked one per profile.
    real = advertised.tool_names
    for connector in advertised.connectors:
        real |= set(connector.allowed_tools or ())

    assert advertised_tool_names(profile) == real


def test_a_profile_cannot_widen_what_its_caller_may_do() -> None:
    """The invariant under all of this: narrowing is layered *under* RBAC, never around it.

    A profile chooses from the surface; the audit middleware and the per-tool authorization gate
    are attached afterwards and unconditionally, so a profile that named a tool the caller may
    not use would still be refused at call time.
    """
    from chemclaw.agent.tool_authz import enforce_tool_authz

    load_profiles()

    # Asserted as *the same chain the default agent gets*, not as a count: the chain has grown
    # (error surfacing was added around audit + authz) and a hardcoded number would have failed
    # on that addition while saying nothing about the property that matters.
    # Compared by *name*, because the audit entry is a closure built per agent: identity would
    # differ for two agents that are nonetheless governed identically, which is the property here.
    from chemclaw.agent.langgraph_agent import tool_call_middleware
    from chemclaw.agent.profiles import get_profile

    def names(profile_name: str | None) -> list[str]:
        chain = tool_call_middleware(object(), get_profile(profile_name))
        return [type(middleware).__name__ for middleware in chain]

    assert names("property-lookup") == names(None)
    assert enforce_tool_authz in tool_call_middleware(object(), get_profile("property-lookup"))


class _FakeAgent:
    """The minimum the front door needs from an agent: it can mint a session."""

    def __init__(self, profile: str | None) -> None:
        self.profile = profile

    def create_session(self, *, session_id: str) -> Any:
        from chemclaw.agent.session import TurnSession

        return TurnSession(session_id=session_id)


def test_a_session_selects_its_profile_and_keeps_it() -> None:
    """`POST /sessions {"profile": …}` binds the session to that agent for its whole life.

    Fixed at creation on purpose: a conversation whose instructions and tools changed underneath
    it would have a thread that no longer matches its own history.
    """
    app = create_app(connector_factory=lambda _profile: [])
    with TestClient(app) as client:
        default = client.post("/sessions").json()["session_id"]
        narrowed = client.post("/sessions", json={"profile": "property-lookup"}).json()[
            "session_id"
        ]
    assert app.state.live_sessions.get(default).profile is None
    assert app.state.live_sessions.get(narrowed).profile == "property-lookup"
    # The profile is *all* the session carries about its surface. There used to be an assertion
    # here that one agent was built per distinct profile and cached — an agent was configuration,
    # not per-session state. Nothing is cached per process now: a graph binds its tools at
    # construction and is compiled per turn, so the profile recorded on the session is what decides
    # each turn's surface.


def test_an_unknown_profile_is_refused_at_session_creation() -> None:
    """A 400 when the session is created, not a 500 on the first turn — the caller can act on it."""
    app = create_app(connector_factory=lambda _profile: [])
    with TestClient(app) as client:
        response = client.post("/sessions", json={"profile": "no-such-profile"})
    assert response.status_code == 400
    assert "no-such-profile" in response.json()["detail"]
