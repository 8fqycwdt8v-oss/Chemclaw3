"""`make connector-validate`, tested for the failures it exists to catch.

Each test asserts one rejection against a manifest built in the test, since a validator passing
on the shipped bundles cannot be told from one that stopped checking. The checks are the ones no
per-file schema can make:

- a job that cannot be built (an unresolvable `params_model`);
- an `inline_wait_seconds` at or beyond the deployment's turn timeout;
- a `connector_urls` key naming no bundle, which would fall back to an unreachable dev default.
"""

from pathlib import Path
from unittest import mock

import pytest
from mcp.server.fastmcp import FastMCP

from chemclaw.cli.validate_connectors import (
    _connector_urls_problems,
    _job_problems,
    _orphan_content_problems,
)
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
    """An inline wait at or beyond the turn timeout is refused.

    The turn would be killed before the wait returns, so every call would look like a timeout.
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


def test_a_connector_urls_key_that_names_no_bundle_is_reported() -> None:
    """A `connector_urls` key that names no bundle is reported.

    Its runtime symptom looks like a transient outage, so it must surface as a configuration error.
    """
    discovered_names = {"calc", "qm", "bo"}
    with mock.patch("chemclaw.cli.validate_connectors.settings") as mock_settings:
        mock_settings.connector_urls = {"calc": "http://override:8000", "typo_bundle": "http://foo"}
        problems = _connector_urls_problems(discovered_names)
    assert any("typo_bundle" in problem for problem in problems)
    assert len(problems) == 1


def test_connector_urls_keys_that_name_real_bundles_are_accepted() -> None:
    """The passing case: all configured URLs name discovered bundles."""
    discovered_names = {"calc", "qm", "bo"}
    with mock.patch("chemclaw.cli.validate_connectors.settings") as mock_settings:
        mock_settings.connector_urls = {"calc": "http://override:8000", "bo": "http://bo:9999"}
        problems = _connector_urls_problems(discovered_names)
    assert problems == []


def test_empty_connector_urls_is_accepted() -> None:
    """No overrides configured: the check passes trivially."""
    discovered_names = {"calc", "qm", "bo"}
    with mock.patch("chemclaw.cli.validate_connectors.settings") as mock_settings:
        mock_settings.connector_urls = {}
        problems = _connector_urls_problems(discovered_names)
    assert problems == []


def test_a_served_tool_the_manifest_never_declares_is_reported() -> None:
    """A served tool the manifest never declares is reported.

    This rule reads the running server; a tool on none of the manifest lists violates nothing the
    YAML checks can see, and an undeclared tool is reachable without appearing in review.
    """
    from chemclaw.cli.validate_connectors import _served_tool_problems

    served = FastMCP("probe")

    @served.tool()
    async def probe_tool(value: str) -> str:
        """The declared read tool."""
        return value

    @served.tool()
    async def index_probe(record_id: str) -> str:
        """A write tool the manifest does not name anywhere."""
        return record_id

    with mock.patch(
        "chemclaw.connectors.registry.importlib.import_module",
        return_value=mock.Mock(server=served),
    ):
        problems = _served_tool_problems(ConnectorManifest.model_validate(_MANIFEST))
    assert len(problems) == 1, problems
    assert "index_probe" in problems[0]
    assert "served on /mcp" in problems[0]


def test_a_bundle_serving_exactly_what_it_declares_is_accepted() -> None:
    """A bundle serving exactly what it declares is accepted.

    `state_changing` and `read_only` must be subsets of `tools`, so the schema cannot express
    "served but not agent-facing"; the comparison is therefore against `tools`.
    """
    from chemclaw.cli.validate_connectors import _served_tool_problems

    served = FastMCP("probe")

    @served.tool()
    async def probe_tool(value: str) -> str:
        """The one declared tool."""
        return value

    with mock.patch(
        "chemclaw.connectors.registry.importlib.import_module",
        return_value=mock.Mock(server=served),
    ):
        assert _served_tool_problems(ConnectorManifest.model_validate(_MANIFEST)) == []


def test_the_manifest_cannot_classify_a_tool_it_does_not_serve() -> None:
    """The manifest cannot classify a tool it does not serve.

    Pins the constraint the served-tool rule rests on.
    """
    with pytest.raises(ValueError, match="does not serve"):
        ConnectorManifest.model_validate(
            {
                **_MANIFEST,
                "endpoint": {
                    **_MANIFEST["endpoint"],  # type: ignore[dict-item]
                    "state_changing": ["index_probe"],
                },
            }
        )


def test_a_job_only_bundle_with_no_server_module_is_not_a_violation() -> None:
    """`qm` serves no MCP surface at all — its capability is a Temporal workflow behind `jobs:`."""
    from chemclaw.cli.validate_connectors import _served_tool_problems

    absent = ModuleNotFoundError("No module named 'chemclaw.connectors.probe.server'")
    absent.name = "chemclaw.connectors.probe.server.tools"
    with mock.patch("chemclaw.connectors.registry.importlib.import_module", side_effect=absent):
        assert _served_tool_problems(ConnectorManifest.model_validate(_MANIFEST)) == []


def test_a_bundle_whose_server_module_is_broken_is_reported_not_skipped() -> None:
    """A transitive import failure is reported, not read as "this bundle serves nothing".

    Otherwise a missing dependency would make the rule pass vacuously, and other import-time
    exceptions would escape as a traceback.
    """
    from chemclaw.cli.validate_connectors import _served_tool_problems

    manifest = ConnectorManifest.model_validate(_MANIFEST)
    missing_dep = ModuleNotFoundError("No module named 'rdkit'")
    missing_dep.name = "rdkit"
    for failure in (missing_dep, ImportError("cannot import name 'foo'"), AttributeError("boom")):
        with mock.patch(
            "chemclaw.connectors.registry.importlib.import_module", side_effect=failure
        ):
            problems = _served_tool_problems(manifest)
        assert len(problems) == 1, f"{failure!r} produced {problems}"
        assert "probe" in problems[0]


def test_a_server_module_with_no_server_object_is_reported_not_skipped() -> None:
    """A server module with no `server` object is reported, not skipped.

    That is a rename, not a bundle without an MCP surface; skipping it would check nothing.
    """
    from chemclaw.cli.validate_connectors import _served_tool_problems

    renamed = mock.Mock(spec=["mcp"])  # a module with no `server` attribute at all
    with mock.patch("chemclaw.connectors.registry.importlib.import_module", return_value=renamed):
        problems = _served_tool_problems(ConnectorManifest.model_validate(_MANIFEST))
    assert len(problems) == 1, problems
    assert "defines no `server`" in problems[0]


def test_a_declared_tool_the_server_does_not_serve_is_reported() -> None:
    """A declared tool the server does not serve is reported.

    `tools:` feeds `available_tool_names()`, which the skill, template and prose validators resolve
    through, so a phantom tool would pass every gate and fail at call time.
    """
    from chemclaw.cli.validate_connectors import _served_tool_problems

    served = FastMCP("probe")

    @served.tool()
    async def probe_tool(value: str) -> str:
        """The one tool that really is served."""
        return value

    manifest = ConnectorManifest.model_validate(
        {
            **_MANIFEST,
            "endpoint": {
                **_MANIFEST["endpoint"],  # type: ignore[dict-item]
                "tools": ["probe_tool", "phantom_search"],
                "read_only": ["probe_tool", "phantom_search"],
            },
        }
    )
    with mock.patch(
        "chemclaw.connectors.registry.importlib.import_module",
        return_value=mock.Mock(server=served),
    ):
        problems = _served_tool_problems(manifest)
    assert len(problems) == 1, problems
    assert "phantom_search" in problems[0]
    assert "does not serve it" in problems[0]


def test_a_declared_but_unserved_tool_is_unverifiable_for_a_bundle_we_do_not_run() -> None:
    """A declared tool is unverifiable for a bundle this repository does not run.

    A bundle with an endpoint and no local `server/` cannot be asked offline what it serves, so its
    tools are reported as unverified rather than as problems.
    """
    from chemclaw.cli.validate_connectors import _served_tool_problems, unverified_tool_surfaces
    from chemclaw.connectors.registry import discovered

    manifest = ConnectorManifest.model_validate(_MANIFEST)
    absent = ModuleNotFoundError("No module named 'chemclaw.connectors.probe.server'")
    absent.name = "chemclaw.connectors.probe.server.tools"
    with mock.patch("chemclaw.connectors.registry.importlib.import_module", side_effect=absent):
        assert _served_tool_problems(manifest) == []
    # Derived rather than typed out: a bundle is unverifiable here exactly when it declares an
    # endpoint and ships no `server/` package.
    expected = {
        name
        for name, (bundle, manifest) in discovered().items()
        if manifest.endpoint is not None and not (bundle / "server").is_dir()
    }
    assert expected, "no declared-not-run bundle in the tree, so this test proves nothing"
    unverified = unverified_tool_surfaces()
    assert set(unverified) == expected, unverified
    # One concrete tool, so the mapping is not merely present but populated.
    assert "screen_hazards" in unverified["safety"]


def test_a_skills_directory_beside_no_connector_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Judgment kept beside a manifest someone else owns must name a connector that exists.

    A directory with no `connector.yaml` of its own is found by name; if the manifest is renamed
    or withdrawn there, its `skills/` would stop loading with no error. A directory holding
    neither `skills/` nor `profiles/` is not a claim and is left alone.
    """
    (tmp_path / "alpha" / "skills" / "judgment").mkdir(parents=True)
    (tmp_path / "ghost" / "skills" / "judgment").mkdir(parents=True)
    (tmp_path / "ghost" / "profiles").mkdir()
    (tmp_path / "notes").mkdir()
    monkeypatch.setattr(settings, "connectors_dir", str(tmp_path))

    problems = _orphan_content_problems({"alpha"})

    assert len(problems) == 1, problems
    assert str(tmp_path / "ghost") in problems[0]
    assert "['skills', 'profiles']" in problems[0]
