"""Discovery, enablement and what the agent ends up advertising.

A repo ships every connector; a deployment enables the subset it has validated. An enabled name
that resolves to nothing must be loud. Bundles are written to `tmp_path`, so nothing depends on
which connectors ship today.
"""

import inspect
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from chemclaw.agent.chemclaw_agent import (
    connector_specs,
    harness_tool_names,
    skill_tool_names,
    subagent_tool_names,
    template_tool_names,
)
from chemclaw.connectors.manifest import HttpEndpoint, StdioEndpoint
from chemclaw.connectors.registry import (
    ConnectorError,
    connector_tool_names,
    declared_connector_tool_names,
    declared_note_types,
    declared_relations,
    declared_skills_dirs,
    discovered,
    enabled,
    forget_discovered,
    health_url,
    job_tools,
    server_tools_module,
    skills_dirs,
)
from chemclaw.core.tool_registry import _REGISTRY
from chemclaw.kg.note import KNOWN_NOTE_TYPES, known_note_types
from chemclaw.kg.relations import KNOWN_RELATIONS, known_relations

_JOB_BLOCK = """
jobs:
  - name: run_thing
    workflow: ThingWorkflow
    summary: Run the thing.
    params:
      - {name: subject, type: string, description: What to run it on.}
"""


def _bundle(root: Path, name: str, body: str) -> Path:
    """Write one bundle directory with `connector.yaml` and return it."""
    bundle = root / name
    bundle.mkdir(parents=True)
    (bundle / "connector.yaml").write_text(body, encoding="utf-8")
    return bundle


def _http_manifest(name: str, port: int = 9001, tools: str = "search") -> str:
    """A minimal valid HTTP-endpoint manifest body."""
    return (
        f"name: {name}\n"
        f"description: the {name} capability\n"
        "endpoint:\n"
        "  transport: http\n"
        f"  url: http://127.0.0.1:{port}/mcp\n"
        f"  health_url: http://127.0.0.1:{port}/healthz\n"
        "  tools:\n"
        f"    - {tools}\n"
        "  read_only:\n"
        f"    - {tools}\n"
    )


def _use(monkeypatch: pytest.MonkeyPatch, root: Path, *, enabled_list: str = "") -> None:
    """Point the registry at `root` as its only connectors dir, with the given enable-list.

    `tests/conftest.py` empties the discovery cache before each test.
    `test_every_ambient_name_space_is_refused_to_a_connector` repoints per arm and clears it itself.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_dir", str(root))
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_enabled", enabled_list)


def test_discovery_finds_bundles_by_folder_and_ignores_everything_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle is a folder with a manifest; a folder without one is not a half-broken connector."""
    _bundle(tmp_path, "alpha", _http_manifest("alpha"))
    (tmp_path / "notes").mkdir()  # a directory with no connector.yaml
    (tmp_path / "README.md").write_text("not a bundle", encoding="utf-8")
    _use(monkeypatch, tmp_path)
    assert list(discovered()) == ["alpha"]


def test_discovery_order_is_sorted_not_filesystem_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tool order is part of the prompt, so it must not vary by machine (reproducibility)."""
    for name in ("zulu", "alpha", "mike"):
        _bundle(tmp_path, name, _http_manifest(name))
    _use(monkeypatch, tmp_path)
    assert [manifest.name for manifest in enabled()] == ["alpha", "mike", "zulu"]


def test_an_empty_enable_list_means_every_discovered_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `skills_enabled` default: a fresh checkout runs the full shipped surface."""
    _bundle(tmp_path, "alpha", _http_manifest("alpha"))
    _bundle(tmp_path, "beta", _http_manifest("beta"))
    _use(monkeypatch, tmp_path)
    assert {manifest.name for manifest in enabled()} == {"alpha", "beta"}


def test_the_enable_list_narrows_and_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-empty list is both the subset *and* the order, because order is configuration."""
    for name in ("alpha", "beta", "gamma"):
        _bundle(tmp_path, name, _http_manifest(name))
    _use(monkeypatch, tmp_path, enabled_list="gamma:alpha")
    assert [manifest.name for manifest in enabled()] == ["gamma", "alpha"]


def test_an_unknown_enabled_connector_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Advertising nothing silently is the failure this refuses: it looks like a broken tool."""
    _bundle(tmp_path, "alpha", _http_manifest("alpha"))
    _use(monkeypatch, tmp_path, enabled_list="alpha:ghost")
    with pytest.raises(ConnectorError, match="ghost"):
        enabled()


def test_a_manifest_name_must_match_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise it is enabled under one name and looked up under another — the `SKILL.md` rule."""
    _bundle(tmp_path, "alpha", _http_manifest("beta"))
    _use(monkeypatch, tmp_path)
    with pytest.raises(ConnectorError, match="lives in directory"):
        discovered()


def test_malformed_yaml_is_a_named_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parse failure names the file, so it is fixable without reading a traceback."""
    _bundle(tmp_path, "alpha", "name: [unclosed\n")
    _use(monkeypatch, tmp_path)
    with pytest.raises(ConnectorError, match="alpha/connector.yaml"):
        discovered()


# The opening of `Chemclaw3-mcp`'s `manifests-internal/calc/connector.yaml`, verbatim down to the
# key that matters; a literal, so the test needs no sibling checkout.
_BACKEND_MANIFEST = """
mount: backend
name: calc
description: >-
  Fast local calculators, request/response and stateless: GFN2-xTB single-point energies, geometry
  optimization, the xTB pKa predictor and the CREST searches. No calculation cache, no artifact
  store, no calibration ledger and no durable jobs.
endpoint:
  transport: http
  url: http://127.0.0.1:8860/mcp
  health_url: http://127.0.0.1:8860/healthz
  request_timeout: 900
  auth:
    mode: bearer
    token_env: CHEMCLAW_CALC_TOKEN
  tools:
    - compute_xtb_energy
    - calculation_key
  read_only:
    - calculation_key
  state_changing:
    - compute_xtb_energy
"""


def test_a_backend_manifest_is_refused_rather_than_partially_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `mount: backend` manifest is refused rather than partially adopted.

    Both repositories hold a `calc` manifest, and `_bundle_dirs` takes the first on the path
    silently, so the backend's smaller surface could replace this repository's `calc` (cache,
    ledger, artifacts, durable jobs) with no error. `ConnectorManifest`'s `extra="forbid"` refuses
    the `mount` key; relaxing it or adding a `mount` field turns the startup error into a partial
    surface.
    """
    _bundle(tmp_path, "calc", _BACKEND_MANIFEST)
    _use(monkeypatch, tmp_path)
    with pytest.raises(ConnectorError, match="mount") as raised:
        discovered()
    assert "calc/connector.yaml" in str(raised.value), "the error must name the file to fix"


def test_each_transport_builds_its_matching_maf_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dispatch, and the allow-list carried through on both: the boundary is transport-free."""
    _bundle(tmp_path, "remote", _http_manifest("remote", tools="search"))
    _bundle(
        tmp_path,
        "local",
        "name: local\ndescription: a local capability\n"
        "endpoint:\n  transport: stdio\n  command: python\n  args: ['-m', 'x']\n"
        "  tools:\n    - compute\n  read_only:\n    - compute\n",
    )
    _use(monkeypatch, tmp_path)
    monkeypatch.setattr("chemclaw.core.config.settings.connector_stdio_enabled", True)
    # Both transports are built as specs; `open_connector_specs` opens a session from a `Connection`
    # mapping, so the transport shows in the connection rather than the type.
    built = {spec.name: spec for spec in connector_specs()}
    assert built["remote"].connection["transport"] == "streamable_http"
    assert built["local"].connection["transport"] == "stdio"
    assert list(built["remote"].allowed_tools or []) == ["search"]
    assert list(built["local"].allowed_tools or []) == ["compute"]


def test_connector_urls_override_the_manifest_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest ships a dev default; a cluster address belongs to the deployment."""
    _bundle(tmp_path, "alpha", _http_manifest("alpha"))
    _use(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "chemclaw.core.config.settings.connector_urls", {"alpha": "http://alpha.svc:8080/mcp"}
    )
    (spec,) = connector_specs()
    assert spec.connection.get("url") == "http://alpha.svc:8080/mcp"


def test_the_health_probe_follows_the_address_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The health probe follows the address override (D-131).

    The chart always sets `connector_urls`, so probing the manifest's loopback default would report
    every connector unreachable.
    """
    _bundle(tmp_path, "alpha", _http_manifest("alpha"))
    _use(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "chemclaw.core.config.settings.connector_urls",
        {"alpha": "http://alpha-connector.svc:8814/mcp"},
    )
    (manifest,) = enabled()
    assert health_url(manifest) == "http://alpha-connector.svc:8814/healthz"


def test_the_health_probe_follows_an_override_that_moves_the_path_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`chemclaw.cli.connectors_dev` mounts every bundle under one port by name, so the path moves.

    Swapping only the origin would give `/healthz`, which that composite serves as a 404 — the
    reason the dev topology could not tell a killed connector from a mis-probed one.
    """
    _bundle(tmp_path, "alpha", _http_manifest("alpha"))
    _use(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "chemclaw.core.config.settings.connector_urls", {"alpha": "http://127.0.0.1:8810/alpha/mcp"}
    )
    (manifest,) = enabled()
    assert health_url(manifest) == "http://127.0.0.1:8810/alpha/healthz"


def test_a_connector_declaring_no_health_route_stays_unprobed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A third-party MCP server may expose nothing; guessing a path would be a false alarm."""
    _bundle(
        tmp_path,
        "alpha",
        "name: alpha\ndescription: the alpha capability\n"
        "endpoint:\n  transport: http\n  url: http://127.0.0.1:9001/mcp\n"
        "  tools:\n    - search\n  read_only:\n    - search\n",
    )
    _use(monkeypatch, tmp_path)
    (manifest,) = enabled()
    assert health_url(manifest) is None


def test_a_jobs_only_connector_contributes_a_tool_and_no_mcp_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Durable capability needs no endpoint: the launcher is the whole agent-facing surface."""
    _bundle(tmp_path, "thing", f"name: thing\ndescription: durable only\n{_JOB_BLOCK}")
    _use(monkeypatch, tmp_path)
    assert connector_specs() == []
    (tool,) = job_tools()
    assert tool.__name__ == "run_thing"


def test_two_connectors_cannot_claim_one_job_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job name is the authz key, so a collision applies one gate to another's work."""
    _bundle(tmp_path, "alpha", f"name: alpha\ndescription: one\n{_JOB_BLOCK}")
    _bundle(tmp_path, "beta", f"name: beta\ndescription: two\n{_JOB_BLOCK}")
    _use(monkeypatch, tmp_path)
    with pytest.raises(ConnectorError, match="already provides"):
        job_tools()


def test_a_job_cannot_take_the_name_of_another_connectors_endpoint_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job cannot take the name of another connector's endpoint tool.

    `connector_tool_names()` is a set union, so a collision silently drops one tool from the agent's
    surface.
    """
    _bundle(tmp_path, "alpha", _http_manifest("alpha", tools="run_thing"))
    _bundle(tmp_path, "beta", f"name: beta\ndescription: two\n{_JOB_BLOCK}")
    _use(monkeypatch, tmp_path)
    with pytest.raises(ConnectorError, match="already provides as a tool"):
        job_tools()


def test_one_connector_cannot_declare_a_job_and_a_tool_with_one_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Within a bundle too: the old dict keyed by job name would have absorbed this silently."""
    _bundle(tmp_path, "alpha", _http_manifest("alpha", tools="run_thing") + _JOB_BLOCK)
    _use(monkeypatch, tmp_path)
    with pytest.raises(ConnectorError, match="already provides as a tool"):
        job_tools()


def test_a_connector_endpoint_tool_cannot_claim_an_in_process_tool_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connector endpoint tool cannot claim an in-process tool name.

    `ToolNode` keys by name and connector tools are appended after in-process ones, so the connector
    would silently win.
    """
    _bundle(tmp_path, "alpha", _http_manifest("alpha", tools="find_notes"))
    _use(monkeypatch, tmp_path)
    with pytest.raises(ConnectorError, match="an in-process tool"):
        job_tools()


def test_a_connector_job_cannot_claim_an_in_process_tool_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connector job cannot claim an in-process tool name.

    The name would put the core tool into `side_effecting_tools()` and `expensive_actions()`,
    re-classifying it, while the launcher is dropped from the surface.
    """
    _bundle(
        tmp_path,
        "alpha",
        "name: alpha\ndescription: durable only\n"
        + _JOB_BLOCK.replace("name: run_thing", "name: find_notes"),
    )
    _use(monkeypatch, tmp_path)
    with pytest.raises(ConnectorError, match="an in-process tool"):
        job_tools()


def test_a_connector_cannot_claim_an_ambient_tool_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connector cannot claim an ambient tool name.

    `read_file` is a `FilesystemMiddleware` verb, not a `@tool`, so the manifest walk cannot see it;
    a bundle claiming it would shadow the scratchpad.
    """
    _bundle(tmp_path, "alpha", _http_manifest("alpha", tools="read_file"))
    _use(monkeypatch, tmp_path)
    with pytest.raises(ConnectorError, match="a scratchpad file verb"):
        job_tools()


# Each ambient name space beside the reason string `_bound_by_this_process` stamps it with,
# transcribed rather than imported, because it is what an operator reads. The launcher name space is
# included because launchers register after the collision check, so a cold load must still refuse
# a bundle claiming one, with the right reason.
_AMBIENT_NAME_SPACES = (
    (skill_tool_names, "a scratchpad file verb"),
    (harness_tool_names, "a plan-harness tool"),
    (subagent_tool_names, "the subagent spawner"),
    (template_tool_names, "a step-template launcher"),
)


@pytest.mark.parametrize(
    ("source", "reason"), _AMBIENT_NAME_SPACES, ids=lambda v: getattr(v, "__name__", v)
)
def test_every_ambient_name_space_is_refused_to_a_connector(
    source: Callable[[], Iterable[str]],
    reason: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every ambient name space is refused to a connector.

    `_bound_by_this_process` unions several name spaces; each is checked here, including
    `harness_tool_names()`, since a connector claiming `write_todos` would win `tools_by_name` over
    the plan harness. The expectation comes from the name-source functions (the independent side),
    and a vacuity assertion stops an empty source from passing over an empty loop.
    """
    names = sorted(source())
    assert names, f"{source.__name__} yields no name, so this arm asserts nothing"
    unrefused: list[str] = []
    for name in names:
        root = tmp_path / source.__name__ / name
        root.mkdir(parents=True)
        _bundle(root, "alpha", _http_manifest("alpha", tools=name))
        _use(monkeypatch, root)
        forget_discovered()
        try:
            job_tools()
        except ConnectorError as refusal:
            assert reason in str(refusal), (
                f"{name} was refused, but not as {reason!r} — the operator-facing reason and the "
                "name space that owns it have drifted apart"
            )
            continue
        unrefused.append(name)
    assert unrefused == []


def test_a_generated_launcher_is_not_read_back_as_a_collision_on_a_second_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generated launcher is not read back as a collision on a second build.

    Launchers register into the live registry under the name the next build declares, so the second
    build must not treat that as a first-party claim.
    """
    _bundle(tmp_path, "alpha", f"name: alpha\ndescription: durable only\n{_JOB_BLOCK}")
    _use(monkeypatch, tmp_path)
    (launcher,) = job_tools()
    # What the first build would have left behind, undone by `monkeypatch` when this test ends.
    monkeypatch.setitem(_REGISTRY, launcher.__name__, launcher)
    assert [tool.__name__ for tool in job_tools()] == ["run_thing"]


def test_connector_tool_names_spans_endpoints_and_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the skill and prose validators check against: both halves, since both are callable."""
    _bundle(
        tmp_path,
        "alpha",
        _http_manifest("alpha", tools="search") + _JOB_BLOCK,
    )
    _use(monkeypatch, tmp_path)
    assert connector_tool_names() == ["run_thing", "search"]


def test_only_declared_and_present_skill_dirs_are_advertised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only declared and present skill dirs are advertised.

    A missing declared dir is a packaging problem for `make connector-validate`; it degrades the
    skill surface rather than breaking every turn.
    """
    with_skills = _bundle(tmp_path, "alpha", _http_manifest("alpha") + "skills:\n  - judgment\n")
    (with_skills / "skills" / "judgment").mkdir(parents=True)
    _bundle(tmp_path, "beta", _http_manifest("beta") + "skills:\n  - missing\n")
    _bundle(tmp_path, "gamma", _http_manifest("gamma"))
    _use(monkeypatch, tmp_path)
    assert skills_dirs() == [str(with_skills / "skills")]


def test_forgetting_discovery_forgets_every_cache_this_module_keeps() -> None:
    """Forgetting discovery forgets every cache this module keeps.

    `forget_discovered` must clear every `functools.cache` the module defines, or a bundle written
    into a walked directory loads with a stale directory set. Derived from `cache_clear` on module
    attributes, so a new cache is covered automatically; scoped to what this module defines
    (`default_ssl_context` belongs to `core.http`).
    """
    from chemclaw.connectors import registry

    cached = {
        name
        for name, value in vars(registry).items()
        if hasattr(value, "cache_clear") and getattr(value, "__module__", "") == registry.__name__
    }
    assert cached, (
        "no cached function was found in connectors.registry, so this check now proves nothing — "
        "either the caches are gone (delete this) or they stopped being `functools.cache`"
    )
    cleared = {
        line.split(".cache_clear")[0].strip()
        for line in inspect.getsource(registry.forget_discovered).splitlines()
        if ".cache_clear()" in line and not line.strip().startswith("#")
    }
    assert cached <= cleared, (
        f"`forget_discovered` clears {sorted(cleared)} and this module caches {sorted(cached)}. "
        f"{sorted(cached - cleared)} would answer from before a manifest was written into a "
        "directory the registry has already walked, which is the one case that function exists for."
    )


def test_a_shadowed_bundles_content_is_still_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Winning a name collision replaces the tool surface, never the files on disk.

    `Chemclaw3-mcp` ports `safety` under the same name and its manifest declares no `skills:`, while
    `connectors/safety/skills/safety-screening/SKILL.md` lives here. Two things must hold: the
    shadowed directory is read, and the winner's declaration is not the gate. The winner's own
    content comes first.
    """
    private = tmp_path / "private"
    shipped = tmp_path / "shipped"
    # The winner: same name, same tools, and no `skills:` key at all — the fleet's shape exactly.
    _bundle(private, "alpha", _http_manifest("alpha", port=7777))
    # The loser: declares its skill and ships it beside the manifest, as this tree's bundles do.
    shadowed = _bundle(
        shipped, "alpha", _http_manifest("alpha", port=8888) + "skills:\n  - judgment\n"
    )
    (shadowed / "skills" / "judgment").mkdir(parents=True)
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_dir", f"{private}:{shipped}")
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_enabled", "")

    # The surface is the winner's, unchanged: a collision still resolves to one manifest.
    (manifest,) = enabled()
    assert isinstance(manifest.endpoint, HttpEndpoint | StdioEndpoint)
    assert "7777" in str(manifest.endpoint), "the name collision must still resolve to one surface"
    assert not manifest.skills, "the winning manifest is the one that declares no skills"

    # ...and the judgment that shipped beside the losing manifest is still reachable.
    assert str(shadowed / "skills") in skills_dirs(), (
        f"skills_dirs() answered {skills_dirs()}. A bundle that won the name collision while "
        "declaring no skills has silently removed the shadowed bundle's SKILL.md — which is the "
        "safety-screening defect, reproduced. A collision decides which manifest describes the "
        "capability; it does not decide which files exist."
    )

    # And the winner's own content keeps precedence, which is what a collision does decide.
    (private / "alpha" / "skills" / "judgment").mkdir(parents=True)
    assert skills_dirs() == [str(private / "alpha" / "skills"), str(shadowed / "skills")], (
        f"skills_dirs() answered {skills_dirs()}; the winning directory must come first, because "
        "the skills backend resolves a duplicate skill name by root order"
    )


def test_the_first_connectors_dir_wins_a_name_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PATH` precedence: an operator's private bundle dir can override a shipped bundle."""
    private = tmp_path / "private"
    shipped = tmp_path / "shipped"
    _bundle(private, "alpha", _http_manifest("alpha", port=7777))
    _bundle(shipped, "alpha", _http_manifest("alpha", port=8888))
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_dir", f"{private}:{shipped}")
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_enabled", "")
    (manifest,) = enabled()
    assert isinstance(manifest.endpoint, HttpEndpoint | StdioEndpoint)
    assert "7777" in str(manifest.endpoint)


def test_a_bundle_contributes_note_types_without_a_core_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle contributes note types without a core edit.

    A bundle's `note_types:` join the graph vocabulary (D-118), scoped to the enabled set, so a note
    of a disabled bundle's type fails `kg-validate`.
    """
    _bundle(tmp_path, "alpha", _http_manifest("alpha") + "note_types:\n  - assay-result\n")
    _bundle(tmp_path, "beta", _http_manifest("beta") + "note_types:\n  - shelved\n")
    _use(monkeypatch, tmp_path, enabled_list="alpha")

    assert declared_note_types() == frozenset({"assay-result"})
    assert "assay-result" in known_note_types()
    assert "shelved" not in known_note_types(), "a disabled bundle contributes no vocabulary"
    assert "assay-result" not in KNOWN_NOTE_TYPES, "core's own set is unchanged"


def test_a_bundle_contributes_relations_the_same_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The edge-side twin, so `kg-validate` accepts an edge a bundle's notes actually draw."""
    _bundle(tmp_path, "alpha", _http_manifest("alpha") + "relations:\n  - assayed-by\n")
    _use(monkeypatch, tmp_path)

    assert declared_relations() == frozenset({"assayed-by"})
    assert "assayed-by" in known_relations()
    assert "assayed-by" not in KNOWN_RELATIONS


def test_a_malformed_vocabulary_name_is_refused_at_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed vocabulary name is refused at load.

    A note type is a path segment (`knowledge/<type>/<id>.md`), so slashes or capitals would make a
    note that validates and is then unfindable.
    """
    _bundle(tmp_path, "alpha", _http_manifest("alpha") + "note_types:\n  - Assay Result\n")
    _use(monkeypatch, tmp_path)
    with pytest.raises(ConnectorError, match="lowercase hyphenated"):
        discovered()


def test_a_bundle_with_no_server_package_has_no_server_module() -> None:
    """`server_tools_module` returns `None` for a bundle this repository declares and does not run.

    Without a `server/` directory the missing module is the parent package, so `exc.name` is the
    package. Note a leftover `__pycache__` makes the directory a namespace package locally, which
    can hide this.
    """
    assert server_tools_module("chem") is None


def test_a_jobs_only_bundle_has_no_server_module() -> None:
    """The other `None`: `results` declares no endpoint and ships no server, and never has."""
    assert server_tools_module("results") is None


def test_a_bundle_outside_the_installed_package_has_no_server_module() -> None:
    """A bundle outside the installed package has no server module.

    `connectors_dir` is a `PATH`-style list, so a private bundle has no `chemclaw.connectors.<name>`
    package and `exc.name` is the bundle package; that is also "no server module".
    """
    assert server_tools_module("no-such-bundle-ships-here") is None


def test_a_bundle_that_serves_tools_returns_its_module() -> None:
    """The positive case, so the two above cannot pass by the function returning `None` always."""
    module = server_tools_module("calc")
    assert module is not None and hasattr(module, "server")


def test_a_broken_dependency_underneath_a_server_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken dependency underneath a server still raises.

    Swallowing it would let a validator check less while reporting success, which is why the
    predicate is exactly the two expected names.
    """
    import importlib

    real = importlib.import_module

    def _missing_dep(name: str, *args: object, **kwargs: object) -> object:
        if name.endswith(".server.tools"):
            raise ModuleNotFoundError("No module named 'absent_dep'", name="absent_dep")
        return real(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib, "import_module", _missing_dep)
    with pytest.raises(ModuleNotFoundError, match="absent_dep"):
        server_tools_module("calc")


def test_a_stdio_manifest_does_not_launch_its_command_unless_the_deployment_allows_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stdio manifest does not launch its command unless the deployment allows it.

    Discovery is enablement, so a file on `connectors_dir` could otherwise run a command in the chat
    process (holding every bearer token and the database pool) before the handshake. No shipped
    bundle uses stdio; its tests opt in explicitly.
    """
    _bundle(
        tmp_path,
        "local",
        "name: local\ndescription: a local capability\n"
        "endpoint:\n  transport: stdio\n  command: /bin/sh\n  args: ['-c', 'true']\n"
        "  tools:\n    - compute\n  read_only:\n    - compute\n",
    )
    _use(monkeypatch, tmp_path)

    with pytest.raises(ConnectorError, match="disabled by default"):
        connector_specs()

    monkeypatch.setattr("chemclaw.core.config.settings.connector_stdio_enabled", True)
    assert [spec.name for spec in connector_specs()] == ["local"]


def test_an_opt_in_bundle_is_discovered_and_not_enabled_by_silence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An opt-in bundle is discovered and not enabled by silence.

    `default_enabled: false` changes only what an empty enable-list means
    (`D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions`): validators see
    its tool names, and no turn pays for its schemas.
    """
    _bundle(tmp_path, "alpha", _http_manifest("alpha"))
    _bundle(tmp_path, "optin", _http_manifest("optin", tools="mtsr") + "default_enabled: false\n")
    _use(monkeypatch, tmp_path)
    assert set(discovered()) == {"alpha", "optin"}
    assert [manifest.name for manifest in enabled()] == ["alpha"]


def test_an_explicit_enable_list_reaches_an_opt_in_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit enable-list reaches an opt-in bundle.

    Filtering the explicit list by `default_enabled` would make an opt-in bundle unreachable by any
    configuration.
    """
    _bundle(tmp_path, "alpha", _http_manifest("alpha"))
    _bundle(tmp_path, "optin", _http_manifest("optin", tools="mtsr") + "default_enabled: false\n")
    _use(monkeypatch, tmp_path, enabled_list="alpha:optin")
    assert [manifest.name for manifest in enabled()] == ["alpha", "optin"]


def test_a_validator_resolves_an_opt_in_tool_that_no_turn_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A validator resolves an opt-in tool that no turn binds.

    `connector_tool_names` is what a turn can call; `declared_connector_tool_names` is what the tree
    declares, read by `skill-validate`, `prose-validate` and `template-validate`.
    """
    _bundle(tmp_path, "alpha", _http_manifest("alpha"))
    _bundle(tmp_path, "optin", _http_manifest("optin", tools="mtsr") + "default_enabled: false\n")
    _use(monkeypatch, tmp_path)
    assert "mtsr" in declared_connector_tool_names()
    assert "mtsr" not in connector_tool_names()
    assert "search" in connector_tool_names()


def test_an_opt_in_bundles_own_skill_is_still_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An opt-in bundle's own skill is still validated.

    `declared_skills_dirs` reaches further than `skills_dirs`: the agent is not offered judgment
    about tools it cannot call, but CI still validates it.
    """
    bundle = _bundle(
        tmp_path, "optin", _http_manifest("optin", tools="mtsr") + "default_enabled: false\n"
    )
    (bundle / "skills" / "thermal").mkdir(parents=True)
    (bundle / "skills" / "thermal" / "SKILL.md").write_text("---\nname: thermal\n---\n")
    _use(monkeypatch, tmp_path)
    assert skills_dirs() == []
    assert declared_skills_dirs() == [str(bundle / "skills")]


def test_the_five_shipped_process_bundles_are_declared_and_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The five shipped process bundles are declared and off.

    Asserted over the real `connectors_dir`: their schemas would ride on every model call, and
    `tests/test_context_floor.py` holds only while the default is off.
    """
    process_bundles = {"thermalsafety", "kinetics", "unitops", "props", "suitability"}
    assert process_bundles <= set(discovered())
    assert process_bundles.isdisjoint({manifest.name for manifest in enabled()})
