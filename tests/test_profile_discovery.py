"""Profiles authored as files and selected per session.

A profile is a use-case configuration only if it can be written without Python and a caller can
ask for it. The load-bearing assertions: narrowing spans the in-process registry and the
connectors' allow-lists, and a profile attenuates, never authorizes.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chemclaw.agent.chemclaw_agent import advertised_tool_names, connector_specs
from chemclaw.agent.profile_discovery import ProfileError, load_profiles, profile_files
from chemclaw.agent.profiles import (
    _REGISTRY,
    DEFAULT_PROFILE,
    AgentProfile,
    get_profile,
    registered_profile_names,
)
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

    The registry is module state, and `load_profiles` is idempotent, which would hide a leak.
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

    One `tool_names` dial narrows the in-process registry and each connector's allow-list, dropping
    connectors left with nothing.
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
    """`advertised_tool_names` equals what `build_agent` + `connector_tools` actually produce.

    It re-applies the narrowing from manifests to avoid opening connector HTTP clients, so the two
    implementations are compared here, over the full surface and a narrowing profile.
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
    """A profile cannot widen what its caller may do.

    Audit and per-tool authorization are attached after narrowing, unconditionally, so a profile
    naming a forbidden tool is still refused at call time.
    """
    from chemclaw.agent.tool_authz import enforce_tool_authz

    load_profiles()

    # Compared by name against the default agent's chain (the audit entry is a per-agent closure),
    # not by count. A superset in order rather than equality: a narrowing profile adds
    # `refuse_undeclared_writes`, which is more governance; what must never happen is losing an
    # entry. The order is checked because nesting is load-bearing.
    from chemclaw.agent.langgraph_agent import tool_call_middleware
    from chemclaw.agent.profiles import get_profile

    def names(profile_name: str | None) -> list[str]:
        chain = tool_call_middleware(object(), get_profile(profile_name))
        return [type(middleware).__name__ for middleware in chain]

    narrowed, default = names("property-lookup"), names(None)
    assert [name for name in narrowed if name in default] == default, (narrowed, default)
    assert set(narrowed) - set(default) == {"refuse_undeclared_writes"}, narrowed
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
    # The profile is all the session carries about its surface; a graph is compiled per turn, so the
    # recorded profile decides each turn's tools.


def test_an_unknown_profile_is_refused_at_session_creation() -> None:
    """A 400 when the session is created, not a 500 on the first turn — the caller can act on it."""
    app = create_app(connector_factory=lambda _profile: [])
    with TestClient(app) as client:
        response = client.post("/sessions", json={"profile": "no-such-profile"})
    assert response.status_code == 400
    assert "no-such-profile" in response.json()["detail"]


def test_a_profile_file_is_read_through_the_one_bounded_manifest_reader(
    profiles_dir: Path,
) -> None:
    """A profile file is read through `core/manifest_io.read_manifest`, the bounded manifest reader.

    A profile's `instructions` is the system message on every call, outside the context-floor
    ratchet. Three arms: oversized `instructions`, a YAML alias bomb and deep nesting (whose
    `RecursionError` would escape every `except ValueError`) are refused.
    """
    (profiles_dir / "huge.yaml").write_text(f"instructions: {'A' * 500_000}\n")
    with pytest.raises(ProfileError, match="instructions"):
        load_profiles()
    (profiles_dir / "huge.yaml").unlink()

    (profiles_dir / "bomb.yaml").write_text(
        "a: &a [x,x,x,x,x,x,x,x,x]\n"
        "b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]\n"
        "c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]\n"
        "d: &d [*c,*c,*c,*c,*c,*c,*c,*c,*c]\n"
        "e: &e [*d,*d,*d,*d,*d,*d,*d,*d,*d]\n"
        "instructions: [*e,*e,*e,*e,*e,*e,*e,*e,*e]\n"
    )
    with pytest.raises(ProfileError, match="node"):
        load_profiles()
    (profiles_dir / "bomb.yaml").unlink()

    (profiles_dir / "deep.yaml").write_text("a: " + "[" * 2000 + "]" * 2000 + "\n")
    with pytest.raises(ProfileError, match="deeper than"):
        load_profiles()


def _tool_universe() -> frozenset[str]:
    """Every name any shipped profile declares as a tool, plus the in-process registry.

    Separates tool names from snake_case argument or field names in prose. Built from declarations,
    not the live surface, which shrinks when a connector is unreachable and would make the check
    environment-dependent.
    """
    from chemclaw.core.tool_registry import registered_tool_names

    names = set(registered_tool_names())
    for profile in _shipped_profiles():
        names |= set(profile.tool_names or ())
    return frozenset(names)


def _shipped_profiles() -> list[AgentProfile]:
    """Every profile under `data/profiles/`, read through the registry.

    `load_profiles()` returns only what it newly registered, so a second call returns `[]` and a
    guard iterating it would pass vacuously.
    """
    load_profiles()
    shipped = (name for name in registered_profile_names() if name != DEFAULT_PROFILE.name)
    return [get_profile(name) for name in shipped]


def test_no_shipped_profiles_prose_names_a_tool_that_profile_does_not_bind() -> None:
    """No shipped profile's prose names a tool that profile does not bind.

    `instructions_for` passes a profile's own instructions through whole, and a model reads a tool
    name in its prompt as a tool it has. The profiles in `data/profiles/` are this repository's
    text, so they are checked; a site's own profiles are outside this tree.
    """
    universe = _tool_universe()
    offences: dict[str, list[str]] = {}
    for profile in _shipped_profiles():
        declared = set(profile.tool_names or ())
        text = profile.instructions or ""
        if not text or not profile.tool_names:
            continue
        named = {name for name in universe if name in text}
        if missing := sorted(named - declared):
            offences[profile.name] = missing

    assert not offences, (
        "a profile's system prompt names a tool that profile does not bind; a model reads that as "
        f"a tool it has: {offences}"
    )
