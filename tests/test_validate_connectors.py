"""`make connector-validate` — the CI gate, tested for the failures it exists to catch.

A validator with no test is a validator nobody knows still works: it passes on the shipped bundles
either because it is correct or because it stopped checking, and those look identical from the
outside. So each test here asserts one *rejection*, against a manifest built in the test rather than
a shipped one — the shipped bundles are covered by the gate itself being green.

The checks below are the ones no per-file schema can make, which is the whole reason the script
exists on top of pydantic's own validation:

- a job that cannot be *built* (an unresolvable `params_model`), because that failure would
  otherwise surface the first time a chemist called the tool;
- an `inline_wait_seconds` at or beyond the turn timeout, which needs the deployment's config and so
  is invisible to the manifest that declares it;
- a `connector_urls` key that names a non-existent bundle, silently falling back to the manifest's
  dev-loopback default which is unreachable in a cluster.
"""

from unittest import mock

import pytest
from mcp.server.fastmcp import FastMCP

from chemclaw.cli.validate_connectors import _connector_urls_problems, _job_problems
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


def test_a_connector_urls_key_that_names_no_bundle_is_reported() -> None:
    """A typo'd URL override is silently ignored, falling back to an unreachable dev default.

    The symptom (a WARNING plus degraded /readyz) is indistinguishable from a transient outage,
    so this validator forces misconfigured keys to surface as a configuration error in CI rather
    than as an infrastructure problem in production.
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
    """The one rule that reads the running server rather than the YAML.

    Every other check here reads the manifest, which is exactly why an undeclared tool was
    invisible to all of them: `_check_classification` validates the `tools` allow-list against
    `state_changing`/`read_only`, so a tool on none of the three lists violates nothing they can
    see. `molfp` and `rxnfp` each served an `index_*` write tool in that state, and because a
    connector authenticates nothing by design, an anonymous MCP handshake against the real app
    wrote a row into `molecule_fingerprints` — the table the report path cites as lab precedent.

    Built here rather than asserted against a shipped bundle, for this file's stated reason: the
    shipped tree is now clean, so a test that only checked it would pass forever whether or not the
    rule still existed.
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
    """The positive half, and the reason the rule compares against `tools` specifically.

    `_check_classification` refuses a manifest that classifies a tool it does not serve, so
    `state_changing` and `read_only` are constrained to be subsets of `tools`: the schema has no
    way to say "served but not agent-facing". The comment that justified the old gap — "the server
    still exposes it, for the ingestion path" — described a state the manifest cannot express,
    which is why a comment was the only place it was ever written.
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
    """Pins the constraint the rule above rests on, so it cannot be relaxed unnoticed.

    If `state_changing` were ever allowed to name a tool outside `tools`, "declared" would stop
    meaning "on the agent's allow-list" and `_served_tool_problems` would silently start permitting
    an undeclared MCP surface again.
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
    """A *transitive* import failure must not read as "this bundle serves nothing".

    Catching bare `ModuleNotFoundError` meant a missing rdkit — or any renamed dependency — made the
    one rule that reads the running server pass vacuously, for exactly the bundle most likely to be
    misbuilt. And any other import-time exception escaped `validate_connectors()` entirely, so CI
    printed a traceback instead of a problem.
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
    """The same vacuous pass one layer down: the module imports and defines no `server`.

    `getattr(module, "server", None) or return []` treated that exactly like `qm`'s "this bundle
    has no MCP surface", and the two are not the same event. The rule's whole job is to ask the
    running server what it serves; with no `server` object there is nothing to ask, so it reported
    no problems while checking nothing. All six bundles with an endpoint define
    `server = FastMCP(...)`, which means the only way into this state is a rename — the change the
    rule most needs to survive.
    """
    from chemclaw.cli.validate_connectors import _served_tool_problems

    renamed = mock.Mock(spec=["mcp"])  # a module with no `server` attribute at all
    with mock.patch("chemclaw.connectors.registry.importlib.import_module", return_value=renamed):
        problems = _served_tool_problems(ConnectorManifest.model_validate(_MANIFEST))
    assert len(problems) == 1, problems
    assert "defines no `server`" in problems[0]


def test_a_declared_tool_the_server_does_not_serve_is_reported() -> None:
    """The other half of "the two must agree exactly" — the half that was never computed.

    `_served_tool_problems` reported `served - declared` and stopped there, while its own docstring
    stated the rule as an equality. A *phantom* tool — named under `tools:` and classified, served
    by nothing — therefore passed this gate, and then passed the other three as well: `tools:` is
    what feeds `available_tool_names()`, the single set `skill-validate`, `template-validate` and
    `prose-validate` all resolve names through. So a rename that lands in a bundle's
    `connectors/<name>/server/tools.py` and not in its `connector.yaml` is green in CI, and
    advertises a capability that answers "unknown tool" the first time a chemist reaches it — the
    "fails at step four after spending compute" this family exists to prevent.
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
    """What the fix above does *not* cover, made visible instead of silent.

    `chem` and `safety` declare an endpoint and ship no `server/` here — their capability is
    `Chemclaw3-mcp`'s (D-2026-08-09). Nothing offline can ask those servers what they serve, so the
    declared→served direction is unverifiable for them, and reporting every declared tool as a
    phantom would fail the gate on two correct manifests. They are reported as *unverified* rather
    than as problems, the same shape `validate_templates.unchecked_arguments` uses for the argument
    check's identical blind spot.
    """
    from chemclaw.cli.validate_connectors import _served_tool_problems, unverified_tool_surfaces
    from chemclaw.connectors.registry import discovered

    manifest = ConnectorManifest.model_validate(_MANIFEST)
    absent = ModuleNotFoundError("No module named 'chemclaw.connectors.probe.server'")
    absent.name = "chemclaw.connectors.probe.server.tools"
    with mock.patch("chemclaw.connectors.registry.importlib.import_module", side_effect=absent):
        assert _served_tool_problems(manifest) == []
    # **The shipped set is derived, not typed out.** This assertion named `chem` and `safety`
    # because those were the two declared-not-run bundles the day it was written, so wiring a
    # third (`rxnpredict`) turned a correct manifest into a red gate. That is the same defect
    # `tests/test_deploy_chart.py`'s all-disabled arm carried, one file over and found in the same
    # change — a test that enumerates a set the tree owns stops testing its property and starts
    # testing its own vintage.
    #
    # The property is: a bundle is unverifiable here exactly when it declares an endpoint and
    # ships no `server/` package for anything to ask.
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
